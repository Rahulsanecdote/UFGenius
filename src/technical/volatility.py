"""Volatility indicators: ATR, Bollinger Bands, Keltner Channels, Squeeze, Historical Volatility."""

import numpy as np
import pandas as pd


def calculate_volatility_indicators(df: pd.DataFrame) -> dict:
    """Calculate all volatility indicators. Returns dict of named pd.Series."""
    if df.empty or len(df) < 21:
        return {}

    indicators = {}

    # True Range & ATR
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)

    for period in [7, 14, 21]:
        indicators[f"ATR_{period}"] = tr.ewm(com=period - 1, adjust=False).mean()

    # Bollinger Bands (20-period, 2 std dev)
    bb_mid = df["Close"].rolling(20).mean()
    bb_std = df["Close"].rolling(20).std()
    indicators["BB_UPPER"] = bb_mid + 2 * bb_std
    indicators["BB_LOWER"] = bb_mid - 2 * bb_std
    indicators["BB_MID"]   = bb_mid
    indicators["BB_WIDTH"] = (indicators["BB_UPPER"] - indicators["BB_LOWER"]) / bb_mid.replace(0, np.nan)
    indicators["BB_PCT_B"] = (df["Close"] - indicators["BB_LOWER"]) / \
                              (indicators["BB_UPPER"] - indicators["BB_LOWER"]).replace(0, np.nan)

    # Keltner Channels (EMA 20, ATR 14)
    kc_mid = df["Close"].ewm(span=20, adjust=False).mean()
    indicators["KC_UPPER"] = kc_mid + 2 * indicators["ATR_14"]
    indicators["KC_LOWER"] = kc_mid - 2 * indicators["ATR_14"]

    # Squeeze: Bollinger Bands inside Keltner Channels
    indicators["SQUEEZE"] = (
        (indicators["BB_UPPER"] < indicators["KC_UPPER"]) &
        (indicators["BB_LOWER"] > indicators["KC_LOWER"])
    )

    # Historical Volatility (20-day annualized)
    log_ret = np.log(df["Close"] / df["Close"].shift(1))
    indicators["HV_20"] = log_ret.rolling(20).std() * np.sqrt(252) * 100

    return indicators


def score_volatility(indicators: dict, current_price: float) -> dict:
    """
    Volatility scoring — used primarily to assess risk, not direction.

    Returns a risk-level label and suggested ATR-based stop distance.
    """
    if not indicators:
        return {"risk_level": "UNKNOWN", "atr": None, "squeeze": False}

    def last(key):
        s = indicators.get(key)
        if s is None:
            return None
        if isinstance(s, pd.Series):
            v = s.iloc[-1]
            return None if (v != v) else float(v)
        return s

    atr14  = last("ATR_14")
    hv20   = last("HV_20")
    pct_b  = last("BB_PCT_B")
    sq_val = indicators.get("SQUEEZE")
    squeeze = bool(sq_val.iloc[-1]) if sq_val is not None and not sq_val.empty else False

    atr_pct = (atr14 / current_price * 100) if (atr14 and current_price) else None

    if hv20 is None:
        risk_level = "UNKNOWN"
    elif hv20 > 60:
        risk_level = "EXTREME"
    elif hv20 > 40:
        risk_level = "HIGH"
    elif hv20 > 20:
        risk_level = "MODERATE"
    else:
        risk_level = "LOW"

    return {
        "risk_level": risk_level,
        "atr": round(atr14, 4) if atr14 else None,
        "atr_pct": round(atr_pct, 2) if atr_pct else None,
        "hv_20": round(hv20, 1) if hv20 else None,
        "pct_b": round(pct_b, 2) if pct_b else None,
        "squeeze": squeeze,
        "squeeze_note": "Breakout imminent (BB inside KC) 🔥" if squeeze else "",
    }
