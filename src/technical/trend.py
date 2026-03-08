"""Trend indicators: SMA, EMA, VWAP, Ichimoku, MACD, Parabolic SAR."""

import numpy as np
import pandas as pd


def calculate_parabolic_sar(
    df: pd.DataFrame,
    af_start: float = 0.02,
    af_max: float = 0.2,
) -> pd.Series:
    """Compute Parabolic SAR."""
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n = len(close)

    sar = np.zeros(n)
    ep = np.zeros(n)
    af = np.zeros(n)
    trend = np.zeros(n)  # 1 = uptrend, -1 = downtrend

    # Initialize
    trend[0] = 1
    sar[0] = low[0]
    ep[0] = high[0]
    af[0] = af_start

    for i in range(1, n):
        prev_sar = sar[i - 1]
        prev_ep = ep[i - 1]
        prev_af = af[i - 1]
        prev_trend = trend[i - 1]

        if prev_trend == 1:  # Uptrend
            sar[i] = prev_sar + prev_af * (prev_ep - prev_sar)
            sar[i] = min(sar[i], low[i - 1], low[max(0, i - 2)])

            if low[i] < sar[i]:  # Trend reversal
                trend[i] = -1
                sar[i] = prev_ep
                ep[i] = low[i]
                af[i] = af_start
            else:
                trend[i] = 1
                ep[i] = max(prev_ep, high[i])
                af[i] = min(prev_af + af_start, af_max) if high[i] > prev_ep else prev_af
        else:  # Downtrend
            sar[i] = prev_sar + prev_af * (prev_ep - prev_sar)
            sar[i] = max(sar[i], high[i - 1], high[max(0, i - 2)])

            if high[i] > sar[i]:  # Trend reversal
                trend[i] = 1
                sar[i] = prev_ep
                ep[i] = high[i]
                af[i] = af_start
            else:
                trend[i] = -1
                ep[i] = min(prev_ep, low[i])
                af[i] = min(prev_af + af_start, af_max) if low[i] < prev_ep else prev_af

    return pd.Series(sar, index=df.index)


def calculate_trend_indicators(df: pd.DataFrame) -> dict:
    """
    Calculate all trend-following indicators.

    Returns dict of named pd.Series.
    """
    if df.empty or len(df) < 26:
        return {}

    indicators = {}

    # Moving Averages
    for period in [8, 20, 50, 100, 200]:
        indicators[f"SMA_{period}"] = df["Close"].rolling(period).mean()
        indicators[f"EMA_{period}"] = df["Close"].ewm(span=period, adjust=False).mean()

    # VWAP — rolling 20-day (daily bars: one bar = one session, so we use a
    # 20-session rolling window instead of a stale cumulative-from-data-start).
    _vwap_window = 20
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    indicators["VWAP"] = (
        (tp * df["Volume"]).rolling(_vwap_window).sum()
        / df["Volume"].rolling(_vwap_window).sum()
    )

    # Ichimoku Cloud
    high_9  = df["High"].rolling(9).max()
    low_9   = df["Low"].rolling(9).min()
    high_26 = df["High"].rolling(26).max()
    low_26  = df["Low"].rolling(26).min()
    high_52 = df["High"].rolling(52).max()
    low_52  = df["Low"].rolling(52).min()

    indicators["tenkan_sen"]  = (high_9 + low_9) / 2
    indicators["kijun_sen"]   = (high_26 + low_26) / 2
    indicators["senkou_a"]    = ((indicators["tenkan_sen"] + indicators["kijun_sen"]) / 2).shift(26)
    indicators["senkou_b"]    = ((high_52 + low_52) / 2).shift(26)
    indicators["chikou_span"] = df["Close"].shift(-26)

    # MACD (12, 26, 9)
    ema_12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["Close"].ewm(span=26, adjust=False).mean()
    indicators["MACD_line"]   = ema_12 - ema_26
    indicators["MACD_signal"] = indicators["MACD_line"].ewm(span=9, adjust=False).mean()
    indicators["MACD_hist"]   = indicators["MACD_line"] - indicators["MACD_signal"]

    # Parabolic SAR
    indicators["PSAR"] = calculate_parabolic_sar(df)

    return indicators


def score_trend(indicators: dict, current_price: float) -> dict:
    """
    Score trend strength 0-100.

    Points breakdown:
    Price > SMA_200:         +20
    Price > SMA_50:          +15
    Price > SMA_20:          +10
    SMA_50 > SMA_200:        +15 (golden cross territory)
    MACD_hist > 0:           +15
    MACD_line > MACD_signal: +10
    Price > VWAP:            +10
    Price > PSAR:             +5
    """
    if not indicators:
        return {"score": 50, "max": 100, "reasons": ["Insufficient data"]}

    score = 0
    reasons = []

    def last(key):
        s = indicators.get(key)
        if s is None or s.empty:
            return None
        v = s.iloc[-1]
        return None if (v != v) else float(v)  # NaN check

    sma200 = last("SMA_200")
    sma50  = last("SMA_50")
    sma20  = last("SMA_20")

    if sma200 and current_price > sma200:
        score += 20
        reasons.append("Above 200 SMA ✅")
    if sma50 and current_price > sma50:
        score += 15
        reasons.append("Above 50 SMA ✅")
    if sma20 and current_price > sma20:
        score += 10
        reasons.append("Above 20 SMA ✅")
    if sma50 and sma200 and sma50 > sma200:
        score += 15
        reasons.append("Golden Cross Active ✅")

    macd_hist = last("MACD_hist")
    macd_line = last("MACD_line")
    macd_sig  = last("MACD_signal")

    if macd_hist is not None and macd_hist > 0:
        score += 15
        reasons.append("MACD Histogram Positive ✅")
    if macd_line is not None and macd_sig is not None and macd_line > macd_sig:
        score += 10
        reasons.append("MACD Bullish Crossover ✅")

    vwap = last("VWAP")
    if vwap and current_price > vwap:
        score += 10
        reasons.append("Above VWAP ✅")

    psar = last("PSAR")
    if psar and current_price > psar:
        score += 5
        reasons.append("Above Parabolic SAR ✅")

    return {"score": score, "max": 100, "reasons": reasons}
