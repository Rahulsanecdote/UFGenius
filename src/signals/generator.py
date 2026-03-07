"""
Master signal generator — orchestrates all analysis modules into a composite score.

Composite Score Weights:
    Technical (trend + momentum):  35%
    Volume:                        20%
    Sentiment (news+social+insider): 20%
    Fundamental:                   15%
    Macro regime:                  10%
"""

from src.data.fetcher import fetch_ohlcv, fetch_ticker_info
from src.fundamental.scorer import calculate_fundamental_score
from src.macro.regime import detect_market_regime
from src.sentiment.insider import analyze_insider_activity
from src.sentiment.news import analyze_news_sentiment
from src.sentiment.social import analyze_social_sentiment
from src.signals.filters import run_disqualification_filters
from src.technical.momentum import calculate_momentum_indicators, score_momentum
from src.technical.support_resistance import calculate_support_resistance
from src.technical.trend import calculate_trend_indicators, score_trend
from src.technical.volatility import calculate_volatility_indicators
from src.technical.volume import calculate_volume_indicators, score_volume
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Signal thresholds
SIGNAL_MAP = [
    (80, "STRONG_BUY",  "VERY_HIGH"),
    (65, "BUY",         "HIGH"),
    (50, "WEAK_BUY",    "MODERATE"),
    (40, "HOLD",        "LOW"),
    (25, "WEAK_SELL",   "MODERATE"),
    (10, "SELL",        "HIGH"),
    (0,  "STRONG_SELL", "VERY_HIGH"),
]

WEIGHTS = config.SIGNAL_WEIGHTS


def generate_signal(ticker: str, macro_regime: dict | None = None) -> dict:
    """
    Run full multi-dimensional analysis and return a signal dict.

    Args:
        ticker:       Stock ticker symbol.
        macro_regime: Pre-computed regime dict (avoid re-fetching when scanning many tickers).

    Returns a dict with: ticker, signal, confidence, score, scores, reasons, disqualifiers.
    """
    log.info(f"Analyzing {ticker} ...")

    # ── Market data ────────────────────────────────────────────────────────
    df = fetch_ohlcv(ticker, period="1y")
    if df.empty or len(df) < 30:
        return _error_signal(ticker, "Insufficient price data")

    current_price = float(df["Close"].iloc[-1])

    # ── Technical ──────────────────────────────────────────────────────────
    trend_ind = calculate_trend_indicators(df)
    mom_ind   = calculate_momentum_indicators(df)
    vol_ind   = calculate_volatility_indicators(df)
    volm_ind  = calculate_volume_indicators(df)

    trend_score  = score_trend(trend_ind, current_price)
    mom_score    = score_momentum(mom_ind)
    volume_score = score_volume(volm_ind)

    # Combined technical score (trend 65%, momentum 35%)
    technical_combined = trend_score["score"] * 0.65 + mom_score["score"] * 0.35

    # ── Fundamental ────────────────────────────────────────────────────────
    fundamental = calculate_fundamental_score(ticker)

    # ── Sentiment ──────────────────────────────────────────────────────────
    info = fetch_ticker_info(ticker)
    company_name = info.get("longName", ticker)

    news    = analyze_news_sentiment(ticker, company_name)
    social  = analyze_social_sentiment(ticker)
    insider = analyze_insider_activity(ticker)

    # Composite sentiment (news 50%, social 30%, insider 20%)
    sentiment_score = (
        news["sentiment_score_0_100"]    * 0.50 +
        social["sentiment_score_0_100"]  * 0.30 +
        insider["insider_score"]         * 0.20
    )

    # ── Macro ──────────────────────────────────────────────────────────────
    if macro_regime is None:
        macro_regime = detect_market_regime()

    # Normalise regime_score (-100..+100) to 0..100
    macro_score_norm = (macro_regime.get("regime_score", 0) + 100) / 2

    # ── Composite ──────────────────────────────────────────────────────────
    w = WEIGHTS
    composite = (
        technical_combined                     * w.get("technical",   0.35) +
        volume_score["score"]                  * w.get("volume",      0.20) +
        sentiment_score                        * w.get("sentiment",   0.20) +
        fundamental["fundamental_score"]       * w.get("fundamental", 0.15) +
        macro_score_norm                       * w.get("macro",       0.10)
    )

    # Regime multiplier — never fight the macro
    size_mult = macro_regime["strategy"]["position_size_multiplier"]
    adjusted  = composite * (0.7 + 0.3 * size_mult)

    # ── Disqualifiers ──────────────────────────────────────────────────────
    disqualifiers = run_disqualification_filters(ticker, df, fundamental)

    # ── Signal classification ──────────────────────────────────────────────
    if disqualifiers:
        signal, confidence = "FILTERED_OUT", "N/A"
    else:
        signal, confidence = _classify(adjusted)

    # ── Support/Resistance ─────────────────────────────────────────────────
    sr = calculate_support_resistance(df, current_price)

    # ── Reasons ────────────────────────────────────────────────────────────
    reasons = (
        trend_score["reasons"] +
        mom_score["reasons"][:2] +
        volume_score["reasons"][:2] +
        [f"News Sentiment: {news['signal']}"] +
        [f"Social Sentiment: {social['signal']}"] +
        insider["flags"][:2] +
        [f"Piotroski F-Score: {fundamental['piotroski_f_score']}/9"] +
        [f"Macro: {macro_regime['regime']}"]
    )

    return {
        "ticker":          ticker,
        "signal":          signal,
        "confidence":      confidence,
        "score":           round(adjusted, 1),
        "raw_composite":   round(composite, 1),
        "current_price":   current_price,
        "scores": {
            "technical":   round(technical_combined, 1),
            "momentum":    round(mom_score["score"], 1),
            "volume":      round(volume_score["score"], 1),
            "sentiment":   round(sentiment_score, 1),
            "fundamental": fundamental["fundamental_score"],
            "macro":       round(macro_score_norm, 1),
        },
        "disqualifiers": disqualifiers,
        "reasons":       reasons,
        "support_resistance": sr,
        "volatility":    calculate_volatility_indicators(df),
        "_df":           df,  # Pass df to avoid re-fetching in trade plan
    }


def _classify(score: float) -> tuple:
    for threshold, sig, conf in SIGNAL_MAP:
        if score >= threshold:
            return sig, conf
    return "STRONG_SELL", "VERY_HIGH"


def _error_signal(ticker: str, reason: str) -> dict:
    return {
        "ticker":          ticker,
        "signal":          "ERROR",
        "confidence":      "N/A",
        "score":           0.0,
        "raw_composite":   0.0,
        "current_price":   None,
        "scores":          {},
        "disqualifiers":   [reason],
        "reasons":         [reason],
        "support_resistance": {},
        "volatility":      {},
        "_df":             None,
    }
