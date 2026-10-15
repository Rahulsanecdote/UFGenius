"""Early-momentum detection: flag names BEFORE they extend.

The movers path (movers.py) only sees a stock once it is already on a gainers
list and up >= movers.min_change_pct, then waits for the 8-cycle rediscovery
and 5m enrichment — structurally late. This module reads 1m bars and fires
while the move is still SMALL:

  * rvol_accel  - mean volume of the last ``accel_bars`` / mean of the prior
                  ``base_bars`` (participation arriving now, not over the day)
  * pressing    - last close within ``press_pct`` of a reference level:
                  session high, opening-range high, premarket high, prior-day high
  * coiled      - precursor compression (src/signals/precursor.coil_present)

and only when ``min_change_pct <= |change| <= max_change_pct`` so names that
already ran are excluded. Long-side only. Advisory discovery; never orders.

Three entry points, all OFF unless ``movers.early.enabled``:
  * ``evaluate_early``   — the pure rule (also used by movers._enrich_candidate)
  * ``WatchlistScanner`` — a fixed watchlist evaluated every worker cycle from
                           ONE batched multi-symbol bars request per ~100 names
  * ``BarBuffer``        — rolling per-symbol 1m bars fed by the price stream's
                           bar subscription; evaluates on each bar close
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime, time as dtime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)
_ET = ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)


# ── the rule ─────────────────────────────────────────────────────────────────

def evaluate_early(df: pd.DataFrame, change_pct: float,
                   prior_high: Optional[float] = None,
                   premarket_high: Optional[float] = None) -> Optional[dict]:
    """Early verdict for a 1m frame, or None when disabled / not measurable."""
    if not config.EARLY_ALERTS_ENABLED or df is None or df.empty:
        return None
    if abs(change_pct) > config.EARLY_MAX_CHANGE_PCT:
        return None
    a, b = int(config.EARLY_ACCEL_BARS), int(config.EARLY_BASE_BARS)
    if len(df) < a + b:
        return None
    vol = df["volume"].astype(float)
    base = vol.iloc[-(a + b):-a].mean()
    accel = float(vol.iloc[-a:].mean() / base) if base > 0 else 0.0
    last = float(df["close"].iloc[-1])
    or_n = max(1, int(config.EARLY_OPENING_RANGE_BARS))
    levels = {"session_high": float(df["high"].iloc[:-1].max()),
              "opening_range_high": float(df["high"].iloc[:or_n].max())}
    if premarket_high:
        levels["premarket_high"] = float(premarket_high)
    if prior_high:
        levels["prior_day_high"] = float(prior_high)
    tol = float(config.EARLY_PRESS_PCT) / 100.0
    pressing = [k for k, lvl in levels.items() if lvl > 0 and last >= lvl * (1 - tol)]
    try:
        from src.signals.precursor import coil_present
        coiled = bool(coil_present(df))
    except Exception:
        coiled = False
    ok = (accel >= float(config.EARLY_MIN_RVOL_ACCEL)
          and change_pct >= float(config.EARLY_MIN_CHANGE_PCT)
          and bool(pressing or coiled))
    return {"fire": bool(ok), "rvol_accel": round(accel, 2),
            "pressing": pressing, "coiled": coiled,
            "levels": {k: round(v, 4) for k, v in levels.items()}}


def early_score(verdict: dict) -> float:
    """0-100 display score for an early hit (accel + level confluence)."""
    s = min(60.0, float(verdict.get("rvol_accel") or 0) * 12.0)
    s += 10.0 * len(verdict.get("pressing") or []) + (10.0 if verdict.get("coiled") else 0.0)
    return round(min(100.0, s), 1)


# ── reference levels ─────────────────────────────────────────────────────────

def _et_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")      # fetcher documents naive UTC
    return idx.tz_convert(_ET)


def split_session(df: pd.DataFrame, now: Optional[datetime] = None):
    """(today's regular-session bars, today's premarket high or None)."""
    if df is None or df.empty:
        return df, None
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_ET).date()
    et = _et_index(df)
    is_today = et.date == today
    pre = is_today & (et.time < _RTH_OPEN)
    rth = is_today & (et.time >= _RTH_OPEN)
    pm_high = float(df["high"][pre].max()) if pre.any() else None
    session = df[rth] if rth.any() else df[is_today]
    return session, pm_high


def prior_day_levels(daily: Optional[pd.DataFrame],
                     now: Optional[datetime] = None) -> tuple[Optional[float], Optional[float]]:
    """(prior close, prior high) from the last COMPLETED daily bar."""
    if daily is None or daily.empty:
        return None, None
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_ET).date()
    done = daily[_et_index(daily).date < today]
    if done.empty:
        return None, None
    row = done.iloc[-1]
    return float(row["close"]), float(row["high"])


# ── watchlist ────────────────────────────────────────────────────────────────

def load_watchlist() -> list[str]:
    """Configured tickers + file entries, upper-cased, de-duplicated, capped."""
    names = list(config.EARLY_WATCHLIST or [])
    path = config.EARLY_WATCHLIST_FILE
    if path and os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                names.extend(t for t in line.replace(",", " ").split())
    out: list[str] = []
    for n in names:
        t = str(n).upper().strip()
        if t and t not in out:
            out.append(t)
    cap = max(0, int(config.EARLY_WATCHLIST_MAX))
    if cap and len(out) > cap:
        log.warning(f"early watchlist: {len(out)} names, capped to {cap}")
        out = out[:cap]
    return out


def _candidate(ticker: str, df: pd.DataFrame, change_pct: float, verdict: dict,
               source: str):
    from src.scanner.movers import MoverCandidate, _last_bar_time
    c = MoverCandidate(ticker=ticker, price=float(df["close"].iloc[-1]),
                       change_pct=round(change_pct, 2), direction="long",
                       sources=[source])
    c.early = verdict
    c.enriched = True
    c.bars_as_of = _last_bar_time(df)
    c.score = c.base_score = early_score(verdict)
    return c


class WatchlistScanner:
    """Evaluates a fixed watchlist every cycle from batched bar requests.

    Bars: one ``fetch_ohlcv_batch(..., interval="1m")`` per cycle — Alpaca's
    multi-symbol endpoint, ~100 symbols per request. Daily context (prior
    close/high) is fetched once per ET day. ``fetch_bars`` / ``fetch_daily``
    are injectable for tests.
    """

    def __init__(self, symbols: Optional[list[str]] = None, *,
                 fetch_bars: Optional[Callable] = None,
                 fetch_daily: Optional[Callable] = None) -> None:
        self.symbols = symbols if symbols is not None else load_watchlist()
        self._fetch_bars = fetch_bars or self._default_bars
        self._fetch_daily = fetch_daily or self._default_daily
        self._ctx: dict[str, dict] = {}
        self._ctx_day = None

    @staticmethod
    def _default_bars(symbols):
        from src.data.fetcher import fetch_ohlcv_batch
        return fetch_ohlcv_batch(symbols, period="1d", interval="1m", use_cache=False)

    @staticmethod
    def _default_daily(symbols):
        from src.data.fetcher import fetch_ohlcv_batch
        return fetch_ohlcv_batch(symbols, period="10d", interval="1d")

    def context(self, symbol: str) -> dict:
        return self._ctx.get(symbol.upper(), {})

    def _refresh_context(self, now: datetime) -> None:
        day = now.astimezone(_ET).date()
        if self._ctx_day == day:
            return
        try:
            daily = self._fetch_daily(self.symbols) or {}
        except Exception as exc:
            log.debug(f"early watchlist: daily context failed ({type(exc).__name__})")
            return
        for s in self.symbols:
            pc, ph = prior_day_levels(daily.get(s), now)
            self._ctx[s] = {"prior_close": pc, "prior_high": ph}
        self._ctx_day = day

    def scan(self, now: Optional[datetime] = None) -> tuple[list, dict]:
        """(firing candidates, {symbol: today's session frame}) for this cycle."""
        if not config.EARLY_ALERTS_ENABLED or not self.symbols:
            return [], {}
        now = now or datetime.now(timezone.utc)
        self._refresh_context(now)
        try:
            frames = self._fetch_bars(self.symbols) or {}
        except Exception as exc:
            log.warning(f"early watchlist: bars fetch failed ({type(exc).__name__})")
            return [], {}
        hits, sessions = [], {}
        for s in self.symbols:
            session, pm_high = split_session(frames.get(s), now)
            if session is None or session.empty:
                continue
            sessions[s] = session
            ctx = self._ctx.setdefault(s, {})
            if pm_high:
                ctx["premarket_high"] = pm_high
            c = evaluate_symbol(s, session, ctx, source="early_watchlist")
            if c is not None:
                hits.append(c)
        return hits, sessions


def evaluate_symbol(symbol: str, df: pd.DataFrame, ctx: dict, *, source: str):
    """Candidate when the early rule fires for ``df`` given its context."""
    prior_close = ctx.get("prior_close")
    if not prior_close:
        return None                       # no reference → change % unknowable
    change = (float(df["close"].iloc[-1]) / prior_close - 1.0) * 100.0
    v = evaluate_early(df, change, prior_high=ctx.get("prior_high"),
                       premarket_high=ctx.get("premarket_high"))
    if not v or not v.get("fire"):
        return None
    return _candidate(symbol, df, change, v, source)


# ── stream bars ──────────────────────────────────────────────────────────────

class BarBuffer:
    """Rolling per-symbol 1m bars from the stream; evaluates on each close."""

    def __init__(self, maxlen: Optional[int] = None) -> None:
        self._maxlen = maxlen or int(config.EARLY_BUFFER_BARS)
        self._bars: dict[str, deque] = {}

    def seed(self, symbol: str, df: pd.DataFrame) -> None:
        """Warm a symbol from REST bars so the first streamed bar is measurable."""
        if df is None or df.empty or symbol in self._bars:
            return
        dq = deque(maxlen=self._maxlen)
        for ts, r in df.tail(self._maxlen).iterrows():
            dq.append((pd.Timestamp(ts), float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"]), float(r["volume"])))
        self._bars[symbol] = dq

    def add(self, bar: dict) -> Optional[pd.DataFrame]:
        sym = str(bar["symbol"]).upper()
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        dq = self._bars.setdefault(sym, deque(maxlen=self._maxlen))
        if dq and dq[-1][0] >= ts:
            return None                   # duplicate / out-of-order bar
        dq.append((ts, float(bar["open"]), float(bar["high"]), float(bar["low"]),
                   float(bar["close"]), float(bar["volume"])))
        return self.frame(sym)

    def frame(self, symbol: str) -> Optional[pd.DataFrame]:
        dq = self._bars.get(symbol.upper())
        if not dq:
            return None
        df = pd.DataFrame(list(dq), columns=["ts", "open", "high", "low", "close", "volume"])
        return df.set_index("ts")

    def symbols(self) -> list[str]:
        return list(self._bars)

    def on_bars(self, bars: list[dict], context: Callable[[str], dict],
                now: Optional[datetime] = None) -> list:
        """Append each streamed bar; return candidates whose rule fired."""
        hits = []
        now = now or datetime.now(timezone.utc)
        for bar in bars:
            df = self.add(bar)
            if df is None:
                continue
            session, _ = split_session(df, now)
            if session is None or session.empty:
                continue
            c = evaluate_symbol(str(bar["symbol"]).upper(), session,
                                context(str(bar["symbol"]).upper()), source="early_stream")
            if c is not None:
                hits.append(c)
        return hits
