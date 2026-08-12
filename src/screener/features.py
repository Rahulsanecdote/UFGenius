"""Per-ticker feature extraction for the screener presets.

Pure pandas/numpy — computes exactly the fields the presets need from a daily
OHLCV frame (+ best-effort fundamentals), no heavier scoring. Every field is
optional: insufficient history yields ``None`` for that field, and the preset
evaluator treats a missing *technical* field as "does not qualify" and a missing
*fundamental* field as "skip that criterion" (see ``evaluate_preset``).
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd


def _last(series: pd.Series) -> Optional[float]:
    if series is None or len(series) == 0:
        return None
    v = series.iloc[-1]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sma(close: pd.Series, period: int) -> Optional[float]:
    if len(close) < period:
        return None
    return _last(close.rolling(period).mean())


def _rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    # Wilder's RSI via EWM(com=period-1) — matches src/technical/momentum.py so
    # the screener's RSI agrees with the rest of the bot.
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return _last(100 - (100 / (1 + rs)))


def _coerce(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def compute_screen_features(context: Any) -> dict:
    """Extract screen features from a `SignalContext` (or any object exposing
    ``price_df`` / ``fundamentals_raw`` / ``ticker_info``).

    Returns a dict with `None` for any field that can't be computed. Never raises.
    """
    feat: dict[str, Any] = {
        "ticker": getattr(context, "ticker", None),
        "price": None, "sma20": None, "sma50": None, "sma200": None,
        "rsi14": None, "avg_volume_20": None, "last_volume": None,
        "rel_volume": None, "high_50": None, "is_new_high_50": None,
        "pct_change_1d": None, "market_cap": None, "roe_pct": None,
        "debt_equity": None, "bars": 0,
    }
    try:
        df = getattr(context, "price_df", None)
        if df is None or getattr(df, "empty", True):
            return feat
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        feat["bars"] = int(len(df))
        feat["price"] = _last(close)
        feat["sma20"] = _sma(close, 20)
        feat["sma50"] = _sma(close, 50)
        feat["sma200"] = _sma(close, 200)
        feat["rsi14"] = _rsi(close, 14)

        if len(volume) >= 20:
            avg20 = _last(volume.rolling(20).mean())
            feat["avg_volume_20"] = avg20
            feat["last_volume"] = _last(volume)
            if avg20 and avg20 > 0 and feat["last_volume"] is not None:
                feat["rel_volume"] = feat["last_volume"] / avg20

        if len(close) >= 2 and close.iloc[-2] != 0:
            feat["pct_change_1d"] = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)

        # New 50-day high: today's price is the max close of the trailing window.
        window = min(50, len(close))
        if window >= 2:
            hi = float(close.iloc[-window:].max())
            feat["high_50"] = hi
            if feat["price"] is not None:
                feat["is_new_high_50"] = feat["price"] >= hi - 1e-9

        # ── Fundamentals (best-effort; None when unavailable) ────────────────
        fr = getattr(context, "fundamentals_raw", None) or {}
        info = getattr(context, "ticker_info", None) or {}
        feat["market_cap"] = _coerce(fr.get("market_cap") or info.get("marketCap"))
        roe = _coerce(info.get("returnOnEquity"))
        feat["roe_pct"] = roe * 100 if roe is not None else None
        # yfinance reports debtToEquity as a PERCENT (e.g. 45.3 = 0.453x); expose
        # a ratio. None when absent — the evaluator then skips that criterion.
        de = _coerce(info.get("debtToEquity"))
        feat["debt_equity"] = de / 100.0 if de is not None else None
    except Exception:  # feature extraction must never break a scan
        return feat
    return feat
