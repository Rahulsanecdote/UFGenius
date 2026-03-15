"""
Master signal generator — orchestrates all analysis modules into a composite score.

Composite Score Weights:
    Technical (trend + momentum):      35%
    Volume:                            20%
    Sentiment (news+social+insider):   20%
    Fundamental:                       15%
    Macro regime:                      10%
"""

from __future__ import annotations

import pandas as pd

from src.core.contracts import TickerSnapshotProvider
from src.features.policies import resolve_signal_weights
from src.features.signal_features import compute_signal_features
from src.fundamental.scorer import calculate_fundamental_score
from src.macro.regime import detect_market_regime
from src.sentiment.insider import analyze_insider_activity
from src.sentiment.news import analyze_news_sentiment
from src.sentiment.social import analyze_social_sentiment
from src.signals.context import SignalContext, build_signal_context
from src.signals.filters import run_disqualification_filters
from src.technical.support_resistance import calculate_support_resistance
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Signal thresholds loaded from config (config.yaml: signal_thresholds)
# Each entry: [min_score, signal_name, confidence_label]
SIGNAL_MAP = [tuple(row) for row in config.SIGNAL_THRESHOLDS]

WEIGHTS = config.SIGNAL_WEIGHTS


def _is_default_news_sentiment(payload: dict) -> bool:
    return (
        isinstance(payload, dict)
        and float(payload.get("sentiment_score_0_100", 0) or 0) == 50.0
        and str(payload.get("signal", "")).upper() == "NEUTRAL"
        and int(payload.get("article_count", 0) or 0) == 0
    )


def _is_default_social_sentiment(payload: dict) -> bool:
    return (
        isinstance(payload, dict)
        and float(payload.get("sentiment_score_0_100", 0) or 0) == 50.0
        and str(payload.get("signal", "")).upper() == "NEUTRAL"
        and int(payload.get("mention_count", 0) or 0) == 0
    )


def _is_default_insider_sentiment(payload: dict) -> bool:
    return (
        isinstance(payload, dict)
        and float(payload.get("insider_score", 0) or 0) == 50.0
        and str(payload.get("signal", "")).upper() == "NEUTRAL"
        and int(payload.get("buy_transactions", 0) or 0) == 0
        and int(payload.get("sell_transactions", 0) or 0) == 0
    )


def _neutral_fundamental_score(ticker: str, fundamentals_raw: dict | None = None) -> dict:
    raw = fundamentals_raw if isinstance(fundamentals_raw, dict) else {}
    return {
        "ticker": ticker,
        "market_cap": raw.get("market_cap"),
        "piotroski_f_score": 5,   # Neutral midpoint
        "piotroski_detail": {},
        "altman_z_score": None,
        "valuation": {},
        "growth": {},
        "fundamental_score": 50,  # Neutral fallback on 0-100 scale
        "raw_fundamentals": raw,
    }


def generate_signal(
    ticker: str,
    macro_regime: dict | None = None,
    *,
    context: SignalContext | None = None,
    price_df: pd.DataFrame | None = None,
    ticker_info: dict | None = None,
    provider: TickerSnapshotProvider | None = None,
) -> dict:
    """
    Run full multi-dimensional analysis and return a signal dict.

    Callers can provide a pre-built SignalContext or partial prefetch data to avoid
    duplicate network fetches.
    """
    symbol = ticker.upper()
    log.info(f"Analyzing {symbol} ...")

    if context is None:
        context = build_signal_context(
            symbol,
            price_df=price_df,
            ticker_info=ticker_info,
            provider=provider,
        )
    if context is None or context.price_df.empty or len(context.price_df) < 30:
        return _error_signal(symbol, "Insufficient price data")

    df = context.price_df
    current_price = float(df["Close"].iloc[-1])

    # Fundamental score can be computed from pre-fetched raw fundamentals.
    try:
        fundamental = calculate_fundamental_score(symbol, fundamentals_data=context.fundamentals_raw)
        if not isinstance(fundamental, dict):
            raise TypeError("fundamental score payload was not a dict")
        if not isinstance(fundamental.get("fundamental_score"), (int, float)):
            raise ValueError("fundamental_score missing or non-numeric")
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        log.warning(f"{symbol}: fundamental scoring failed ({exc}); using neutral fallback", exc_info=True)
        fundamental = _neutral_fundamental_score(symbol, context.fundamentals_raw)
        fundamental["_fundamental_fallback"] = True

    # Run disqualifiers early to avoid expensive downstream analysis for invalid tickers.
    disqualifiers = run_disqualification_filters(
        symbol,
        df,
        fundamental_score=fundamental,
        fundamentals_raw=context.fundamentals_raw,
    )
    if disqualifiers:
        return _filtered_signal(
            symbol=symbol,
            current_price=current_price,
            disqualifiers=disqualifiers,
            fundamental=fundamental,
            df=df,
        )

    # ── Technical (Phase 3 feature registry/store) ────────────────────────
    feature_bundle, feature_cache_hit = compute_signal_features(
        ticker=symbol,
        price_df=df,
        current_price=current_price,
    )
    trend_score = feature_bundle["trend_score"]
    mom_score = feature_bundle["momentum_score"]
    volume_score = feature_bundle["volume_score"]
    technical_combined = feature_bundle["technical_combined"]
    vol_ind = feature_bundle["volatility_indicators"]

    # ── Sentiment ──────────────────────────────────────────────────────────
    company_name = context.ticker_info.get("longName", symbol)
    news = analyze_news_sentiment(symbol, company_name)
    social = analyze_social_sentiment(symbol)
    insider = analyze_insider_activity(symbol)

    # Composite sentiment (news 50%, social 30%, insider 20%)
    sentiment_score = (
        news["sentiment_score_0_100"] * 0.50
        + social["sentiment_score_0_100"] * 0.30
        + insider["insider_score"] * 0.20
    )

    # ── Macro ──────────────────────────────────────────────────────────────
    if macro_regime is None:
        macro_regime = detect_market_regime()

    # Normalise regime_score (-100..+100) to 0..100
    macro_score_norm = (macro_regime.get("regime_score", 0) + 100) / 2

    # ── Composite ──────────────────────────────────────────────────────────
    w = resolve_signal_weights(
        WEIGHTS,
        regime=macro_regime.get("regime"),
        asset_class=(context.instrument.asset_class.value if context.instrument is not None else "equity"),
        enable_regime_weighting=config.FEATURE_ENABLE_REGIME_WEIGHTING,
    )
    effective_weights = dict(w)
    _weight_total = sum(
        effective_weights.get(k, 0.0) for k in ("technical", "volume", "sentiment", "fundamental", "macro")
    )
    if not (0.95 <= _weight_total <= 1.05):
        log.warning(f"{symbol}: signal weights sum to {_weight_total:.3f}, expected ~1.0 — check config")

    sentiment_weight_redistributed = False

    all_sentiment_default_neutral = (
        _is_default_news_sentiment(news)
        and _is_default_social_sentiment(social)
        and _is_default_insider_sentiment(insider)
    )
    if all_sentiment_default_neutral and effective_weights.get("sentiment", 0.0) > 0.0:
        effective_weights["sentiment"] = 0.0
        total = sum(max(0.0, float(value)) for value in effective_weights.values())
        if total > 0:
            effective_weights = {
                key: max(0.0, float(value)) / total
                for key, value in effective_weights.items()
            }
            sentiment_weight_redistributed = True
    composite = (
        technical_combined * effective_weights.get("technical", 0.35)
        + volume_score["score"] * effective_weights.get("volume", 0.20)
        + sentiment_score * effective_weights.get("sentiment", 0.20)
        + fundamental["fundamental_score"] * effective_weights.get("fundamental", 0.15)
        + macro_score_norm * effective_weights.get("macro", 0.10)
    )

    # Regime multiplier — dampen risk in weak regimes, lighter impact in neutral.
    # Old: composite * (0.7 + 0.3 * mult) over-penalized neutral conditions.
    # New: composite * (0.8 + 0.2 * mult) keeps bear dampening but reduces neutral drag.
    size_mult = macro_regime.get("strategy", {}).get("position_size_multiplier", 1.0)
    adjusted = composite * (0.8 + 0.2 * size_mult)

    signal, confidence = _classify(adjusted)

    # ── Support/Resistance ─────────────────────────────────────────────────
    sr = calculate_support_resistance(df, current_price)

    # ── Reasons ────────────────────────────────────────────────────────────
    reasons = (
        trend_score["reasons"]
        + mom_score["reasons"][:2]
        + volume_score["reasons"][:2]
        + [f"News Sentiment: {news['signal']}"]
        + [f"Social Sentiment: {social['signal']}"]
        + insider["flags"][:2]
        + [f"Piotroski F-Score: {fundamental['piotroski_f_score']}/9"]
        + [f"Macro: {macro_regime['regime']}"]
    )
    if sentiment_weight_redistributed:
        reasons.append("Sentiment unavailable — weight redistributed")

    return {
        "ticker": symbol,
        "signal": signal,
        "confidence": confidence,
        "score": round(adjusted, 1),
        "raw_composite": round(composite, 1),
        "current_price": current_price,
        "market_cap": fundamental.get("market_cap"),
        "scores": {
            "technical": round(technical_combined, 1),
            "momentum": round(mom_score["score"], 1),
            "volume": round(volume_score["score"], 1),
            "sentiment": round(sentiment_score, 1),
            "fundamental": fundamental["fundamental_score"],
            "macro": round(macro_score_norm, 1),
        },
        "disqualifiers": [],
        "reasons": reasons,
        "support_resistance": sr,
        "volatility": vol_ind,
        "_df": df,
        "_context": context,
        "_provider": context.provider,
        "_feature_cache_hit": feature_cache_hit,
        "_feature_cache_key": feature_bundle.get("feature_cache_key"),
        "_feature_version": feature_bundle.get("feature_version"),
        "_weights": effective_weights,
    }


def _classify(score: float) -> tuple:
    for threshold, sig, conf in SIGNAL_MAP:
        if score >= threshold:
            return sig, conf
    return "STRONG_SELL", "VERY_HIGH"


def _filtered_signal(
    *,
    symbol: str,
    current_price: float,
    disqualifiers: list[str],
    fundamental: dict,
    df: pd.DataFrame,
) -> dict:
    return {
        "ticker": symbol,
        "signal": "FILTERED_OUT",
        "confidence": "N/A",
        "score": 0.0,
        "raw_composite": 0.0,
        "current_price": current_price,
        "market_cap": fundamental.get("market_cap"),
        "scores": {"fundamental": fundamental.get("fundamental_score", 0)},
        "disqualifiers": disqualifiers,
        "reasons": disqualifiers,
        "support_resistance": {},
        "volatility": {},
        "_df": df,
        "_context": None,
    }


def _error_signal(ticker: str, reason: str) -> dict:
    return {
        "ticker": ticker,
        "signal": "ERROR",
        "confidence": "N/A",
        "score": 0.0,
        "raw_composite": 0.0,
        "current_price": None,
        "market_cap": None,
        "scores": {},
        "disqualifiers": [reason],
        "reasons": [reason],
        "support_resistance": {},
        "volatility": {},
        "_df": None,
        "_context": None,
    }
