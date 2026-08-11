"""
Look-ahead & staleness guards for time-indexed OHLCV frames (upgrade plan P1.1).

Adopted from TradingAgents' one genuinely strong idea — *look-ahead hygiene as a
first-class concern*. At intraday frequency the ways to accidentally trade on
information you could not have had are many and cheap to hit:

- a provider returns a **still-forming** or future-labelled bar (its timestamp is
  at/after "now"); acting on it is trading the future,
- bars arrive **out of order or duplicated** at session boundaries or across
  provider fallbacks,
- the frame is **stale** — the feed stalled and the newest bar is many intervals
  old, so "current price" is a lie.

These are pure, deterministic helpers (no I/O) so they are trivially testable and
can be reused by the fetch path, the backtest as-of reads, and the live loop.
All frames here are assumed to carry a naive-UTC DatetimeIndex (the convention
`src/data/fetcher.py` already establishes).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)

# Bar-size → seconds, for the intervals the provider layer supports intraday.
_INTERVAL_SECONDS = {
    "1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
    "60m": 3600, "1h": 3600, "90m": 5400,
}


def interval_seconds(interval: str) -> Optional[int]:
    """Duration of one bar in seconds, or None for daily+/unknown intervals."""
    return _INTERVAL_SECONDS.get(str(interval).lower())


def _utcnow_naive() -> datetime:
    """Naive-UTC 'now', matching the fetcher's naive-UTC DatetimeIndex."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive(ts: pd.Timestamp) -> pd.Timestamp:
    """Coerce a Timestamp to naive UTC so comparisons never raise on tz mismatch."""
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def sort_dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """Return the frame sorted by time-ascending with duplicate timestamps removed.

    Keeps the LAST row for a duplicated timestamp (the freshest print for that
    bar). A non-DatetimeIndex frame is returned untouched (nothing to order).
    """
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df if df is not None else pd.DataFrame()
    out = df.sort_index()
    dup = out.index.duplicated(keep="last")
    if dup.any():
        out = out[~dup]
    return out


def bar_age_seconds(df: pd.DataFrame, now: Optional[datetime] = None) -> Optional[float]:
    """Age in seconds of the newest bar, or None if not computable."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return None
    now = now or _utcnow_naive()
    last = _as_naive(pd.Timestamp(df.index[-1])).to_pydatetime()
    return (now - last).total_seconds()


def drop_future_bars(
    df: pd.DataFrame,
    now: Optional[datetime] = None,
    tolerance_sec: float = 0.0,
) -> pd.DataFrame:
    """Drop bars timestamped after ``now`` (+ tolerance) — future = look-ahead.

    ``tolerance_sec`` absorbs benign clock skew between the provider and this
    host so a bar labelled a few seconds ahead is not discarded spuriously.
    """
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df if df is not None else pd.DataFrame()
    now = now or _utcnow_naive()
    cutoff = pd.Timestamp(now) + pd.Timedelta(seconds=max(0.0, float(tolerance_sec)))
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    keep = idx <= cutoff
    if not keep.all():
        dropped = int((~keep).sum())
        log.debug(f"drop_future_bars: dropped {dropped} future-labelled bar(s)")
        return df[keep]
    return df


def as_of(df: pd.DataFrame, cutoff) -> pd.DataFrame:
    """Return only bars at/before ``cutoff`` — the ``Date <= curr_date`` clamp.

    The backtest/live as-of read: never let a consumer see bars past the moment
    it is meant to be evaluating. ``cutoff`` may be a datetime or ISO string.
    """
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df if df is not None else pd.DataFrame()
    ts = _as_naive(pd.Timestamp(cutoff))
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return df[idx <= ts]


def is_stale(
    df: pd.DataFrame,
    interval: str,
    *,
    max_staleness_intervals: float,
    now: Optional[datetime] = None,
) -> bool:
    """True if the newest bar is older than ``max_staleness_intervals`` bars.

    For daily+/unknown intervals (no bar duration) staleness is not evaluated
    here and this returns False — the intraday layer is what this guards.
    """
    secs = interval_seconds(interval)
    if secs is None:
        return False
    age = bar_age_seconds(df, now=now)
    if age is None:
        return True  # no bars at all is maximally stale
    return age > max_staleness_intervals * secs


def sanitize_intraday(
    df: pd.DataFrame,
    now: Optional[datetime] = None,
    *,
    future_tolerance_sec: float = 0.0,
) -> pd.DataFrame:
    """Order + de-duplicate + drop future bars — the standard intraday cleanup.

    Combines the guards a live intraday frame always needs before use, in the
    right order (order/dedupe first, then the future-bar clamp).
    """
    out = sort_dedupe(df)
    return drop_future_bars(out, now=now, tolerance_sec=future_tolerance_sec)
