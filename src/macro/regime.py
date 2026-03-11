"""Market regime detection: SPY/VIX/breadth/gold/bonds analysis."""

import pandas as pd

from src.data.fetcher import fetch_ohlcv
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

REGIME_STRATEGY = {
    "BULL_RISK_ON":   {"bias": "LONG",   "position_size_multiplier": 1.0},
    "MILD_BULL":      {"bias": "LONG",   "position_size_multiplier": 0.8},
    "NEUTRAL_CHOPPY": {"bias": "NEUTRAL","position_size_multiplier": 0.5},
    "MILD_BEAR":      {"bias": "SHORT",  "position_size_multiplier": 0.3},
    "BEAR_RISK_OFF":  {"bias": "CASH",   "position_size_multiplier": 0.0},
}


def detect_market_regime() -> dict:
    """
    Classify the current market regime and return a strategy multiplier.

    Regime scoring:
        SPY > 200 SMA:            +30
        SPY < 200 SMA:            -30
        VIX < 15:                 +30
        VIX < 20:                 +15
        VIX 20-30:                -10
        VIX > 30:                 -30
        >70% stocks above 200SMA: +20
        <30% stocks above 200SMA: -20
        Safe haven flows:         -15

    Regime thresholds:
        score ≥  50 → BULL_RISK_ON
        score ≥  20 → MILD_BULL
        score ≥ -20 → NEUTRAL_CHOPPY
        score ≥ -50 → MILD_BEAR
        else        → BEAR_RISK_OFF
    """
    try:
        return _compute_regime()
    except Exception as e:
        log.error(f"Regime detection error: {e}")
        return _fallback_regime()


def _compute_regime() -> dict:
    spy = _download("SPY",  period="1y")
    vix = _download("^VIX", period="3mo")
    tlt = _download("TLT",  period="3mo")
    gld = _download("GLD",  period="3mo")

    # SPY is required — if we can't get it, fall back to neutral
    if spy.empty or len(spy) < 50:
        log.warning("SPY data unavailable or insufficient — falling back to neutral regime")
        return _fallback_regime()

    spy_price   = float(spy["Close"].iloc[-1])
    spy_sma200  = float(spy["Close"].rolling(200).mean().iloc[-1]) if len(spy) >= 200 else spy_price
    spy_sma50   = float(spy["Close"].rolling(50).mean().iloc[-1])  if len(spy) >= 50  else spy_price
    current_vix = float(vix["Close"].iloc[-1]) if not vix.empty else 20.0

    regime_score = 0
    regime_flags = []

    # ── SPY trend ──────────────────────────────────────────────────────────
    spy_vs_200 = (spy_price / spy_sma200 - 1) * 100
    if spy_price > spy_sma200:
        regime_score += 30
        regime_flags.append(f"SPY above 200 SMA (+{spy_vs_200:.1f}%) — Bull Trend ✅")
    else:
        regime_score -= 30
        regime_flags.append(f"SPY below 200 SMA ({spy_vs_200:.1f}%) — Bear Trend ⚠️")

    # ── VIX fear gauge ─────────────────────────────────────────────────────
    if current_vix < 15:
        regime_score += 30
        regime_flags.append(f"VIX {current_vix:.1f} — Low Fear ✅")
    elif current_vix < 20:
        regime_score += 15
        regime_flags.append(f"VIX {current_vix:.1f} — Normal ✅")
    elif current_vix < 30:
        regime_score -= 10
        regime_flags.append(f"VIX {current_vix:.1f} — Elevated ⚠️")
    else:
        regime_score -= 30
        regime_flags.append(f"VIX {current_vix:.1f} — HIGH FEAR 🚨")

    # ── Market breadth proxy (SPY vs SMA50 momentum) ───────────────────────
    # True breadth requires NYSE A/D data — approximate with SPY internal trend
    spy_above_50 = spy_price > spy_sma50
    if spy_above_50:
        regime_score += 10
        regime_flags.append("SPY above 50 SMA — Breadth Healthy ✅")
    else:
        regime_score -= 10
        regime_flags.append("SPY below 50 SMA — Breadth Weak ⚠️")

    # ── Safe haven flows ───────────────────────────────────────────────────
    if not tlt.empty and not gld.empty:
        tlt_trend = float(tlt["Close"].pct_change(20).iloc[-1]) if len(tlt) > 20 else 0
        gld_trend = float(gld["Close"].pct_change(20).iloc[-1]) if len(gld) > 20 else 0
        if tlt_trend > 0.05 and gld_trend > 0.05:
            regime_score -= 15
            regime_flags.append("Safe haven flows (Gold+Bonds up) — Risk-Off ⚠️")

    # ── 10-year Treasury yield (FRED, optional) ────────────────────────────
    ten_yr = _fetch_ten_year_yield()
    if ten_yr is not None:
        if ten_yr > 5.0:
            regime_score -= 10
            regime_flags.append(f"10-Yr Yield {ten_yr:.2f}% — High Rate Pressure ⚠️")
        elif ten_yr < 3.5:
            regime_score += 5
            regime_flags.append(f"10-Yr Yield {ten_yr:.2f}% — Supportive ✅")

    # ── Classify ───────────────────────────────────────────────────────────
    if regime_score >= 50:
        regime = "BULL_RISK_ON"
    elif regime_score >= 20:
        regime = "MILD_BULL"
    elif regime_score >= -20:
        regime = "NEUTRAL_CHOPPY"
    elif regime_score >= -50:
        regime = "MILD_BEAR"
    else:
        regime = "BEAR_RISK_OFF"

    return {
        "regime":        regime,
        "regime_score":  regime_score,
        "flags":         regime_flags,
        "vix":           round(current_vix, 2),
        "spy_vs_200":    round(spy_vs_200, 2),
        "ten_yr_yield":  ten_yr,
        "strategy":      REGIME_STRATEGY[regime],
    }


def _download(ticker: str, period: str = "1y") -> "pd.DataFrame":
    try:
        return fetch_ohlcv(ticker, period=period, interval="1d")
    except Exception as e:
        log.debug(f"Failed to download {ticker}: {e}")
        return pd.DataFrame()


def _fetch_ten_year_yield() -> float | None:
    """Fetch US 10-year treasury yield from FRED (requires FRED_API_KEY)."""
    if not config.FRED_API_KEY:
        return None
    try:
        from fredapi import Fred
        fred = Fred(api_key=config.FRED_API_KEY)
        series = fred.get_series("GS10")
        return float(series.iloc[-1]) if not series.empty else None
    except Exception as e:
        log.debug(f"FRED 10-yr yield error: {e}")
        return None


def _fallback_regime() -> dict:
    """Return a conservative neutral regime when data is unavailable."""
    return {
        "regime":       "NEUTRAL_CHOPPY",
        "regime_score": 0,
        "flags":        ["Data unavailable — defaulting to neutral"],
        "vix":          None,
        "spy_vs_200":   None,
        "ten_yr_yield": None,
        "strategy":     REGIME_STRATEGY["NEUTRAL_CHOPPY"],
    }
