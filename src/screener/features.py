"""Per-ticker feature extraction for the screener presets.

Pure pandas/numpy — computes exactly the fields the presets need from a daily
OHLCV frame (+ best-effort fundamentals), no heavier scoring. Lookback periods
are config-driven (`screener.sma_periods` / `rsi_period` / `volume_lookback` /
`high_lookback`), never hardcoded. Every field is optional: insufficient history
yields ``None`` for that field, and the preset evaluator treats a missing
*technical* field as "does not qualify" and a missing *fundamental* field as
"skip that criterion" (see ``evaluate_preset``).
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.utils import config


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


def _rsi(close: pd.Series, period: int) -> Optional[float]:
    # Wilder's RSI via EWM(com=period-1) — matches src/technical/momentum.py.
    # A window with gains and no losses is RSI 100 (not NaN/None), so an RSI
    # *minimum* criterion isn't spuriously failed on a pure uptrend.
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    return _last(rsi)


def _coerce(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ratio(num: Any, den: Any) -> Optional[float]:
    n, d = _coerce(num), _coerce(den)
    if n is None or d is None or d == 0:
        return None
    return n / d


def compute_screen_features(context: Any) -> dict:
    """Extract screen features from a `SignalContext` (or any object exposing
    ``price_df`` / ``fundamentals_raw`` / ``ticker_info``).

    Returns a dict with `None` for any field that can't be computed. Never raises.
    """
    feat: dict[str, Any] = {
        "ticker": getattr(context, "ticker", None),
        "price": None, "rsi": None, "avg_volume": None, "last_volume": None,
        "rel_volume": None, "high": None, "is_new_high": None,
        "pct_change_1d": None, "market_cap": None, "roe_pct": None,
        "debt_equity": None, "bars": 0,
    }
    sma_periods = config.SCREENER_SMA_PERIODS or [20, 50, 200]
    for p in sma_periods:
        feat[f"sma{p}"] = None
    try:
        df = getattr(context, "price_df", None)
        if df is None or getattr(df, "empty", True):
            return feat
        close = df["Close"].astype(float)
        volume = df["Volume"].astype(float)
        feat["bars"] = int(len(df))
        feat["price"] = _last(close)
        for p in sma_periods:
            feat[f"sma{p}"] = _sma(close, int(p))
        feat["rsi"] = _rsi(close, int(config.SCREENER_RSI_PERIOD))

        # Volume: baseline is the PRECEDING N completed bars (exclude the current
        # bar), so rel-volume isn't diluted by the very bar being measured and the
        # liquidity average isn't inflated by a single spike.
        vlb = int(config.SCREENER_VOLUME_LOOKBACK)
        feat["last_volume"] = _last(volume)
        if len(volume) >= vlb + 1:
            prior_avg = _last(volume.iloc[-(vlb + 1):-1])
            feat["avg_volume"] = prior_avg
            if prior_avg and prior_avg > 0 and feat["last_volume"] is not None:
                feat["rel_volume"] = feat["last_volume"] / prior_avg

        if len(close) >= 2 and close.iloc[-2] != 0:
            feat["pct_change_1d"] = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)

        # New N-day high: only decidable with a full window; otherwise None so a
        # `require_new_high` criterion fails on insufficient data.
        hlb = int(config.SCREENER_HIGH_LOOKBACK)
        if len(close) >= hlb:
            hi = float(close.iloc[-hlb:].max())
            feat["high"] = hi
            if feat["price"] is not None:
                feat["is_new_high"] = feat["price"] >= hi - 1e-9

        # ── Fundamentals (best-effort; None when unavailable) ────────────────
        # Prefer the normalized fundamentals (available regardless of provider);
        # fall back to raw yfinance info keys. Deriving ROE / debt-equity from
        # net_income / total_debt / total_equity means the criterion actually
        # works in a broker-configured env whose info payload lacks those keys.
        fr = getattr(context, "fundamentals_raw", None) or {}
        info = getattr(context, "ticker_info", None) or {}
        feat["market_cap"] = _coerce(fr.get("market_cap") or info.get("marketCap"))

        roe_ratio = _ratio(fr.get("net_income"), fr.get("total_equity"))
        if roe_ratio is None:
            info_roe = _coerce(info.get("returnOnEquity"))
            roe_ratio = info_roe  # yfinance returnOnEquity is already a fraction
        feat["roe_pct"] = roe_ratio * 100 if roe_ratio is not None else None

        de = _ratio(fr.get("total_debt"), fr.get("total_equity"))
        if de is None:
            info_de = _coerce(info.get("debtToEquity"))
            # yfinance reports debtToEquity as a PERCENT (e.g. 45.3 = 0.453x).
            de = info_de / 100.0 if info_de is not None else None
        feat["debt_equity"] = de
    except Exception:  # feature extraction must never break a scan
        return feat
    return feat
