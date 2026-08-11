"""
Intraday technical features (upgrade plan P1.3).

Session-anchored intraday measures the intraday entry logic is built on:

- **VWAP** — volume-weighted average price for the *current session* (resets each
  day). Price above VWAP = intraday buyers in control; a reclaim of VWAP is a
  classic long trigger.
- **Opening range** — the high/low of the first N minutes of the session; a break
  above the opening-range high is the canonical breakout entry.
- **Relative volume** — current bar volume vs the session's prior-bar average;
  participation confirmation (a breakout on thin volume is a fakeout).
- **Intraday ATR** — average true range on intraday bars, sized for an
  intraday-scaled stop rather than a daily one.

All functions are pure and session-aware: a multi-day intraday frame is sliced to
the **latest session** (bars sharing the last bar's date) so VWAP and the opening
range don't bleed across days. Frames carry a naive-UTC DatetimeIndex (the
fetcher convention).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def current_session_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Slice to the latest session — bars whose date equals the last bar's date."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df if df is not None else pd.DataFrame()
    last_date = df.index[-1].date()
    return df[df.index.date == last_date]


def vwap(df: pd.DataFrame, session_only: bool = True) -> Optional[float]:
    """Current session VWAP, or None if not computable.

    VWAP = Σ(typical_price · volume) / Σ(volume) over the session, where typical
    price is (H+L+C)/3.
    """
    frame = current_session_bars(df) if session_only else df
    if frame is None or frame.empty:
        return None
    typical = (frame["High"].astype(float) + frame["Low"].astype(float) + frame["Close"].astype(float)) / 3.0
    vol = frame["Volume"].astype(float)
    total_vol = float(vol.sum())
    if total_vol <= 0:
        return None
    return float((typical * vol).sum() / total_vol)


def opening_range(df: pd.DataFrame, minutes: int = 30) -> Optional[dict]:
    """High/low of the first ``minutes`` of the current session.

    Returns ``{"high", "low", "bars"}`` or None when the session is empty.
    Uses the session's first timestamp + ``minutes`` as the window edge, so it
    is bar-size agnostic.
    """
    frame = current_session_bars(df)
    if frame is None or frame.empty:
        return None
    start = frame.index[0]
    window_end = start + pd.Timedelta(minutes=max(1, int(minutes)))
    opening = frame[frame.index < window_end]
    if opening.empty:
        opening = frame.iloc[:1]  # degenerate: at least the first bar
    return {
        "high": float(opening["High"].astype(float).max()),
        "low": float(opening["Low"].astype(float).min()),
        "bars": int(len(opening)),
    }


def relative_volume(df: pd.DataFrame) -> Optional[float]:
    """Current bar volume vs the average of the session's PRECEDING bars.

    Excludes the current bar from the average so a spike doesn't dilute its own
    ratio. None when there is no prior bar to compare against.
    """
    frame = current_session_bars(df)
    if frame is None or len(frame) < 2:
        return None
    vols = frame["Volume"].astype(float)
    prior = vols.iloc[:-1]
    avg = float(prior.mean())
    if avg <= 0:
        return None
    return float(vols.iloc[-1] / avg)


def intraday_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Wilder-style ATR over intraday bars (uses the full frame, not one session).

    ATR needs history across the session boundary to be stable, so this is
    computed on the whole intraday frame. None if there are too few bars.
    """
    if df is None or df.empty or len(df) < period + 1:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    prev_close = df["Close"].astype(float).shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(com=max(1, int(period)) - 1, adjust=False).mean()
    val = float(atr.iloc[-1])
    return val if np.isfinite(val) and val > 0 else None
