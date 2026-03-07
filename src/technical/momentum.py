"""Momentum indicators: RSI, Stochastic, Williams %R, ROC, CCI, divergence detection."""

import numpy as np
import pandas as pd


def calculate_momentum_indicators(df: pd.DataFrame) -> dict:
    """Calculate all momentum indicators. Returns dict of named pd.Series."""
    if df.empty or len(df) < 21:
        return {}

    indicators = {}

    # RSI at multiple timeframes
    for period in [7, 14, 21]:
        delta = df["Close"].diff()
        gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        indicators[f"RSI_{period}"] = 100 - (100 / (1 + rs))

    # Stochastic Oscillator (%K, %D)
    low_14  = df["Low"].rolling(14).min()
    high_14 = df["High"].rolling(14).max()
    denom   = (high_14 - low_14).replace(0, np.nan)
    indicators["STOCH_K"] = 100 * (df["Close"] - low_14) / denom
    indicators["STOCH_D"] = indicators["STOCH_K"].rolling(3).mean()

    # Williams %R
    indicators["WILLIAMS_R"] = -100 * (high_14 - df["Close"]) / denom

    # Rate of Change
    for period in [5, 10, 20]:
        indicators[f"ROC_{period}"] = df["Close"].pct_change(period) * 100

    # Commodity Channel Index (20-period)
    tp      = (df["High"] + df["Low"] + df["Close"]) / 3
    sma_tp  = tp.rolling(20).mean()
    mean_dev = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    indicators["CCI"] = (tp - sma_tp) / (0.015 * mean_dev.replace(0, np.nan))

    # RSI Divergence (14-period)
    indicators["RSI_DIVERGENCE"] = detect_rsi_divergence(
        df["Close"], indicators["RSI_14"], lookback=10
    )

    return indicators


def detect_rsi_divergence(
    prices: pd.Series,
    rsi: pd.Series,
    lookback: int = 10,
) -> pd.Series:
    """
    Return a Series of divergence labels at each bar.

    BULLISH_DIVERGENCE:  Price makes lower low, RSI makes higher low → potential buy.
    BEARISH_DIVERGENCE:  Price makes higher high, RSI makes lower high → potential sell.
    NONE:                No divergence detected.
    """
    labels = pd.Series("NONE", index=prices.index)

    for i in range(lookback, len(prices)):
        recent_prices = prices.iloc[i - lookback: i]
        recent_rsi    = rsi.iloc[i - lookback: i]
        p_cur = prices.iloc[i]
        r_cur = rsi.iloc[i]

        if p_cur > recent_prices.max() and r_cur < recent_rsi.max():
            labels.iloc[i] = "BEARISH_DIVERGENCE"
        elif p_cur < recent_prices.min() and r_cur > recent_rsi.min():
            labels.iloc[i] = "BULLISH_DIVERGENCE"

    return labels


def score_momentum(indicators: dict) -> dict:
    """
    Score momentum 0-100.

    Points breakdown:
    RSI 40-60 (healthy momentum):     +20
    RSI 60-70 (strong momentum):      +30
    RSI <30 (oversold bounce):        +20
    STOCH_K rising, <80:              +20
    ROC_10 positive:                  +15
    CCI 0-100 (bullish zone):         +15
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

    rsi14 = last("RSI_14")
    if rsi14 is not None:
        if 40 <= rsi14 <= 60:
            score += 20
            reasons.append(f"RSI {rsi14:.1f} — Healthy Momentum ✅")
        elif 60 < rsi14 <= 70:
            score += 30
            reasons.append(f"RSI {rsi14:.1f} — Strong Momentum ✅")
        elif rsi14 < 30:
            score += 20
            reasons.append(f"RSI {rsi14:.1f} — Oversold Bounce Setup ✅")
        elif rsi14 > 70:
            score -= 10
            reasons.append(f"RSI {rsi14:.1f} — Overbought ⚠️")

    stoch_k = last("STOCH_K")
    stoch_d = last("STOCH_D")
    if stoch_k is not None and stoch_d is not None:
        if stoch_k < 80 and stoch_k > stoch_d:
            score += 20
            reasons.append(f"Stochastic %K {stoch_k:.1f} — Bullish ✅")
        elif stoch_k < 20:
            score += 15
            reasons.append(f"Stochastic %K {stoch_k:.1f} — Oversold ✅")

    roc10 = last("ROC_10")
    if roc10 is not None and roc10 > 0:
        score += 15
        reasons.append(f"ROC(10) +{roc10:.1f}% — Positive Momentum ✅")

    cci = last("CCI")
    if cci is not None:
        if 0 < cci < 100:
            score += 15
            reasons.append(f"CCI {cci:.1f} — Bullish Zone ✅")
        elif cci >= 100:
            score += 5
            reasons.append(f"CCI {cci:.1f} — Overbought Territory")

    # RSI_DIVERGENCE is a string Series — read directly, don't convert to float
    div_series = indicators.get("RSI_DIVERGENCE")
    div = str(div_series.iloc[-1]) if div_series is not None and len(div_series) > 0 else "NONE"
    if div == "BULLISH_DIVERGENCE":
        score += 15
        reasons.append("RSI Bullish Divergence Detected ✅")
    elif div == "BEARISH_DIVERGENCE":
        score -= 15
        reasons.append("RSI Bearish Divergence Detected ⚠️")

    return {"score": max(0, min(score, 100)), "max": 100, "reasons": reasons}
