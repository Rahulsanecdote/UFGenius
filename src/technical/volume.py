"""Volume indicators: OBV, CMF, RVOL, Accumulation/Distribution Line."""

import numpy as np
import pandas as pd


def calculate_volume_indicators(df: pd.DataFrame) -> dict:
    """Calculate all volume indicators. Returns dict of named pd.Series."""
    if df.empty or len(df) < 20:
        return {}

    indicators = {}

    # On-Balance Volume
    obv = [0]
    closes = df["Close"].values
    volumes = df["Volume"].values
    for i in range(1, len(df)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    indicators["OBV"]     = pd.Series(obv, index=df.index)
    indicators["OBV_EMA"] = indicators["OBV"].ewm(span=20, adjust=False).mean()

    # Chaikin Money Flow (20-period)
    hl_range = (df["High"] - df["Low"]).replace(0, np.nan)
    mf_mult  = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / hl_range
    mf_vol   = mf_mult * df["Volume"]
    vol_sum  = df["Volume"].rolling(20).sum().replace(0, np.nan)
    indicators["CMF"] = mf_vol.rolling(20).sum() / vol_sum

    # Relative Volume (vs 20-day average)
    avg_vol = df["Volume"].rolling(20).mean().replace(0, np.nan)
    indicators["RVOL"] = df["Volume"] / avg_vol

    # Volume Trend: 5-day avg vs 20-day avg
    indicators["VOL_RISING"] = df["Volume"].rolling(5).mean() > df["Volume"].rolling(20).mean()

    # Accumulation / Distribution Line
    indicators["AD_LINE"] = (mf_mult * df["Volume"]).cumsum()

    return indicators


def score_volume(indicators: dict) -> dict:
    """
    Score volume strength 0-100.

    RVOL > 5.0:          +30 (extreme interest)
    RVOL > 2.0:          +20
    RVOL > 1.5:          +10
    OBV > OBV_EMA:       +25 (accumulation)
    CMF > 0.1:           +25 (buying pressure)
    VOL_RISING:          +20
    """
    if not indicators:
        return {"score": 50, "max": 100, "reasons": ["Insufficient data"]}

    score = 0
    reasons = []

    def last(key):
        s = indicators.get(key)
        if s is None:
            return None
        if isinstance(s, pd.Series):
            v = s.iloc[-1]
            return None if (v != v) else float(v)
        return s

    rvol = last("RVOL")
    if rvol is not None:
        if rvol > 5.0:
            score += 30
            reasons.append(f"RVOL {rvol:.1f}x — Extreme Interest 🔥")
        elif rvol > 2.0:
            score += 20
            reasons.append(f"RVOL {rvol:.1f}x — High Interest ✅")
        elif rvol > 1.5:
            score += 10
            reasons.append(f"RVOL {rvol:.1f}x — Above Average")

    obv     = last("OBV")
    obv_ema = last("OBV_EMA")
    if obv is not None and obv_ema is not None and obv > obv_ema:
        score += 25
        reasons.append("OBV > OBV EMA — Accumulation ✅")

    cmf = last("CMF")
    if cmf is not None:
        if cmf > 0.1:
            score += 25
            reasons.append(f"CMF {cmf:.2f} — Strong Buying Pressure ✅")
        elif cmf > 0:
            score += 10
            reasons.append(f"CMF {cmf:.2f} — Mild Buying Pressure")
        elif cmf < -0.1:
            score -= 15
            reasons.append(f"CMF {cmf:.2f} — Selling Pressure ⚠️")

    vol_rising_s = indicators.get("VOL_RISING")
    if vol_rising_s is not None and not vol_rising_s.empty and bool(vol_rising_s.iloc[-1]):
        score += 20
        reasons.append("Volume Trend Rising ✅")

    return {"score": min(score, 100), "max": 100, "reasons": reasons}
