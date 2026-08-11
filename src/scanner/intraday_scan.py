"""
Continuous intraday scanner (upgrade plan P1.2).

Replaces the 6 fixed daily scan slots with a short-interval loop that runs
unusual-volume / momentum / breakout detection over **live intraday bars** (via
the P1.1 ``fetch_intraday`` layer) and emits deduplicated candidates into an
in-process queue for the entry logic (P1.3) to drain.

Design constraints (from the plan): **rate- and cost-aware**. The universe is
capped per cycle, bars come from the interval-scaled cache (so repeated cycles
inside one bar are served from cache, not re-fetched), the loop interval is
floored to a safe minimum, and the loop only runs during market hours.

This module discovers *candidates* only — it deliberately does not size, gate,
or place anything. Turning a candidate into a risk-gated order is P1.3 + the
existing RiskGuard/executor path. Nothing here assumes a candidate is
profitable; it is a shortlist to evaluate, not a signal to buy.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from datetime import time as dtime
from typing import Callable, Optional

import pandas as pd

from src.data.fetcher import fetch_intraday
from src.data.lookahead import is_stale
from src.scanner.candidate_queue import Candidate, CandidateQueue
from src.scanner.gap_scanner import scan_for_gaps
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Hard floor on the loop interval — a tighter cadence just re-reads cached bars
# and hammers the provider on cache misses (rate-limit guard).
_MIN_INTERVAL_SEC = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_market_hours(now_et: Optional[datetime] = None) -> bool:
    """True during regular NYSE hours (Mon-Fri 09:30-16:00 ET).

    Fails OPEN on timezone errors (better to scan than to silently stop), matching
    the executor's monitor-loop convention.
    """
    try:
        now = now_et or datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        return dtime(9, 30) <= now.time() <= dtime(16, 0)
    except Exception:
        return True


def _premarket_start() -> dtime:
    """Configured pre-market scan-window start (ET), tolerant of a bad value."""
    raw = str(config.CONTINUOUS_SCAN_PREMARKET_START_ET or "07:00")
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, TypeError):
        return dtime(7, 0)


def is_scan_window(now_et: Optional[datetime] = None) -> bool:
    """True during the scan window: pre-market start through the regular close.

    Wider than ``is_market_hours`` so the pre-market gapper can surface gaps
    before the open (the plan explicitly includes the pre-market gapper). Fails
    OPEN on timezone errors.
    """
    try:
        now = now_et or datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        return _premarket_start() <= now.time() <= dtime(16, 0)
    except Exception:
        return True


def score_intraday_frame(df: pd.DataFrame) -> Optional[dict]:
    """Compute intraday unusual-volume / momentum / breakout metrics for one frame.

    Returns a dict of metrics (rel_volume, momentum_pct, is_breakout, last_price)
    or None when there are too few bars to measure. Pure: no fetch, no config
    reads beyond the passed frame — thresholding is the caller's job.
    """
    min_bars = int(config.CONTINUOUS_SCAN_MIN_BARS)
    if df is None or df.empty or len(df) < max(2, min_bars):
        return None

    closes = df["Close"].astype(float)
    volumes = df["Volume"].astype(float)
    last_price = float(closes.iloc[-1])

    # Relative volume: current bar vs the average of the *preceding* bars (exclude
    # the current bar so a spike does not dilute its own ratio).
    prior_vol = volumes.iloc[:-1]
    avg_vol = float(prior_vol.mean()) if len(prior_vol) else 0.0
    rel_volume = float(volumes.iloc[-1] / avg_vol) if avg_vol > 0 else 0.0

    # Momentum: % change over the lookback window.
    mlb = max(1, int(config.CONTINUOUS_SCAN_MOMENTUM_LOOKBACK_BARS))
    ref_idx = max(0, len(closes) - 1 - mlb)
    ref_price = float(closes.iloc[ref_idx])
    momentum_pct = ((last_price - ref_price) / ref_price * 100.0) if ref_price > 0 else 0.0

    # Breakout: new high over the lookback window (exclude the current bar so the
    # comparison is against prior structure).
    blb = max(1, int(config.CONTINUOUS_SCAN_BREAKOUT_LOOKBACK_BARS))
    prior_highs = df["High"].astype(float).iloc[-(blb + 1):-1]
    prior_high = float(prior_highs.max()) if len(prior_highs) else float("inf")
    is_breakout = last_price >= prior_high

    return {
        "last_price": round(last_price, 4),
        "rel_volume": round(rel_volume, 2),
        "momentum_pct": round(momentum_pct, 2),
        "is_breakout": bool(is_breakout),
        "prior_high": round(prior_high, 4) if prior_high != float("inf") else None,
        "bars": int(len(df)),
    }


def _candidates_from_metrics(ticker: str, m: dict, now: datetime) -> list[Candidate]:
    """Turn a scored frame into zero or more threshold-crossing candidates."""
    out: list[Candidate] = []
    ts = now.isoformat()
    rel_thr = float(config.CONTINUOUS_SCAN_REL_VOLUME_THRESHOLD)
    mom_thr = float(config.CONTINUOUS_SCAN_MOMENTUM_PCT_THRESHOLD)

    if m["rel_volume"] >= rel_thr:
        out.append(Candidate(ticker, "volume", m["rel_volume"], ts, dict(m)))
    if abs(m["momentum_pct"]) >= mom_thr:
        out.append(Candidate(ticker, "momentum", m["momentum_pct"], ts, dict(m)))
    # A breakout only counts as a candidate when it comes with participation
    # (unusual volume) — a lone breakout on thin volume is a classic fakeout.
    if m["is_breakout"] and m["rel_volume"] >= rel_thr:
        out.append(Candidate(ticker, "breakout", m["last_price"], ts, dict(m)))
    return out


def scan_intraday(
    tickers: list[str],
    now: Optional[datetime] = None,
    fetch: Callable[..., pd.DataFrame] = fetch_intraday,
) -> list[Candidate]:
    """Run one intraday scan pass over ``tickers`` and return candidates.

    ``fetch`` is injectable for testing. Per-ticker failures are isolated so one
    bad symbol never aborts the cycle.
    """
    now = now or _utcnow()
    interval = config.CONTINUOUS_SCAN_INTERVAL
    max_stale = float(config.INTRADAY_MAX_STALENESS_INTERVALS)
    out: list[Candidate] = []
    for ticker in tickers:
        try:
            df = fetch(ticker, interval=interval)
            # Reject stale frames: fetch_intraday can hand back a stale-cache
            # fallback when an upstream refresh fails. Scoring it would stamp a
            # historical spike with the current cycle time and emit it as a live
            # candidate every cycle, poisoning the shortlist.
            if is_stale(df, interval, max_staleness_intervals=max_stale, now=now):
                log.debug(f"{ticker}: intraday frame stale — skipping")
                continue
            metrics = score_intraday_frame(df)
            if metrics is None:
                continue
            out.extend(_candidates_from_metrics(str(ticker).upper(), metrics, now))
        except Exception as exc:  # never let one symbol break the cycle
            log.debug(f"{ticker}: intraday scan error: {exc}")
    return out


def scan_gaps(
    tickers: list[str],
    now: datetime | None = None,
    scan: Callable[..., list[dict]] = scan_for_gaps,
) -> list[Candidate]:
    """Emit ``gap`` candidates from the daily-boundary pre-market gapper.

    Wraps the existing ``scan_for_gaps`` (today's open vs prior close on daily
    bars) and keeps only gaps with real volume participation — consistent with
    the breakout fakeout guard, and the whole point of a *tradable* gap.
    """
    now = now or _utcnow()
    out: list[Candidate] = []
    try:
        gaps = scan(tickers, min_gap_pct=float(config.CONTINUOUS_SCAN_MIN_GAP_PCT))
    except Exception as exc:
        log.debug(f"gap scan error: {exc}")
        return out
    ts = now.isoformat()
    for g in gaps:
        if not g.get("high_volume"):
            continue  # thin-volume gap → skip (fakeout guard)
        out.append(Candidate(str(g["ticker"]).upper(), "gap", float(g["gap_pct"]), ts, dict(g)))
    return out


class ContinuousScanner:
    """Short-interval loop that feeds an intraday CandidateQueue (P1.2).

    Market-hours gated, interval-floored, and universe-capped. The producer half
    of the P1.2/P1.3 pipeline; the consumer drains ``queue``.
    """

    def __init__(
        self,
        tickers: list[str],
        queue: Optional[CandidateQueue] = None,
        interval_sec: Optional[int] = None,
        fetch: Callable[..., pd.DataFrame] = fetch_intraday,
        gap_scan: Callable[..., list[Candidate]] = scan_gaps,
    ) -> None:
        # Retain the FULL universe and rotate through it in capped batches, so a
        # universe larger than the per-cycle cap is fully covered over successive
        # cycles rather than the first N being scanned forever.
        self.universe = [str(t).upper() for t in tickers]
        self._cap = max(1, int(config.CONTINUOUS_SCAN_UNIVERSE_CAP))
        self._offset = 0
        # `is not None`, not truthiness: a fresh CandidateQueue is falsy (len 0),
        # so `queue or CandidateQueue()` would silently discard a caller's shared
        # queue and strand the consumer.
        self.queue = queue if queue is not None else CandidateQueue(
            maxlen=int(config.CONTINUOUS_SCAN_QUEUE_MAX),
            dedup_ttl_sec=float(config.CONTINUOUS_SCAN_DEDUP_TTL_SEC),
        )
        req = interval_sec if interval_sec is not None else config.CONTINUOUS_SCAN_INTERVAL_SEC
        self.interval_sec = max(_MIN_INTERVAL_SEC, int(req))
        self._fetch = fetch
        self._gap_scan = gap_scan
        self._stop = threading.Event()

    def next_batch(self) -> list[str]:
        """Return the next capped batch of the universe, advancing the cursor."""
        n = len(self.universe)
        if n == 0:
            return []
        cap = min(self._cap, n)
        start = self._offset % n
        batch = [self.universe[(start + i) % n] for i in range(cap)]
        self._offset = (start + cap) % n
        return batch

    def run_once(self, now: Optional[datetime] = None) -> int:
        """Run one scan cycle over the next batch; enqueue candidates.

        Runs the intraday unusual-volume/momentum/breakout scanners AND the
        daily-boundary gapper on the batch. Returns the number enqueued.
        """
        now = now or _utcnow()
        batch = self.next_batch()
        candidates = scan_intraday(batch, now=now, fetch=self._fetch)
        candidates += self._gap_scan(batch, now=now)
        enqueued = sum(1 for c in candidates if self.queue.push(c, now=now))
        if enqueued:
            log.info(f"Intraday scan: {enqueued} new candidate(s) queued "
                     f"({len(self.queue)} pending)")
        return enqueued

    def stop(self) -> None:
        """Signal the loop to exit after the current cycle."""
        self._stop.set()

    def run_forever(self, window_check: Callable[[], bool] = is_scan_window) -> None:
        """Loop until ``stop()``: scan each interval during the scan window only."""
        log.info(
            f"Continuous intraday scanner started: {len(self.universe)} tickers "
            f"(<= {self._cap}/cycle), every {self.interval_sec}s, "
            f"interval={config.CONTINUOUS_SCAN_INTERVAL}. Ctrl+C to stop."
        )
        while not self._stop.is_set():
            try:
                if window_check():
                    self.run_once()
                else:
                    log.debug("Intraday scan idle: outside scan window")
            except Exception as exc:
                log.error(f"Continuous scan cycle error: {exc}", exc_info=True)
            # Interruptible sleep so stop() takes effect promptly.
            self._stop.wait(self.interval_sec)
