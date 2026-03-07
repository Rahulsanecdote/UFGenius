"""
Daily scanner — universe filter → technical pre-filter → full signal generation.

Step 1: Load universe (S&P 500 / Russell 1000)
Step 2: Technical pre-filter (RSI, RVOL, proximity to breakout) — fast pass
Step 3: Full signal generation on pre-filtered candidates
Step 4: Sort by composite score and group into strong_buys / buys / watch_list
"""

import time
from datetime import datetime
from typing import Optional

from src.data.fetcher import fetch_ohlcv
from src.data.universe import get_universe
from src.macro.regime import detect_market_regime
from src.signals.generator import generate_signal
from src.signals.trade_plan import generate_trade_plan
from src.technical.momentum import calculate_momentum_indicators
from src.technical.volume import calculate_volume_indicators
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

BUY_SIGNALS = {"STRONG_BUY", "BUY", "WEAK_BUY"}


def technical_pre_filter(tickers: list) -> list:
    """
    Fast technical pre-filter to reduce the universe before expensive API calls.

    Passes a ticker if ALL of:
    - RSI_14 between 35 and 72 (not extreme)
    - RVOL >= 1.3 (above-average volume interest)
    - Enough history (>50 bars)
    """
    passed = []
    log.info(f"Pre-filtering {len(tickers)} tickers ...")

    for ticker in tickers:
        try:
            df = fetch_ohlcv(ticker, period="3mo")
            if df.empty or len(df) < 50:
                continue

            mom = calculate_momentum_indicators(df)
            vol = calculate_volume_indicators(df)

            rsi = mom.get("RSI_14")
            rvol = vol.get("RVOL")

            rsi_val  = float(rsi.iloc[-1])  if rsi  is not None and len(rsi)  > 0 else 50.0
            rvol_val = float(rvol.iloc[-1]) if rvol is not None and len(rvol) > 0 else 1.0

            # Skip NaN
            if rsi_val != rsi_val or rvol_val != rvol_val:
                continue

            if 35 <= rsi_val <= 72 and rvol_val >= 1.3:
                passed.append(ticker)

        except Exception as e:
            log.debug(f"{ticker}: pre-filter error: {e}")
            continue

    log.info(f"Pre-filter: {len(tickers)} → {len(passed)} candidates")
    return passed


def run_daily_scan(
    account_size: Optional[float] = None,
    universe_name: Optional[str] = None,
    max_signals: int = 15,
    pre_filter: bool = True,
) -> dict:
    """
    Run a full daily market scan.

    Args:
        account_size:  Portfolio size (USD). Defaults to config.
        universe_name: "SP500" | "RUSSELL1000". Defaults to config.
        max_signals:   Maximum tickers to run full analysis on.
        pre_filter:    Apply fast technical pre-filter first.

    Returns a structured scan result dict.
    """
    if account_size is None:
        account_size = config.ACCOUNT_SIZE
    if universe_name is None:
        universe_name = config.SCAN_UNIVERSE

    scan_start = datetime.now()
    log.info(f"=== Daily Scan Started: {scan_start.strftime('%Y-%m-%d %H:%M')} ===")

    # ── Market regime check ────────────────────────────────────────────────
    regime = detect_market_regime()
    log.info(f"Market Regime: {regime['regime']} (score={regime['regime_score']})")

    if regime["regime"] == "BEAR_RISK_OFF" and not config.SAFETY.get("trade_in_bear_market", False):
        log.warning("BEAR MARKET DETECTED — no long positions recommended")
        return {
            "scan_date":     scan_start.isoformat(),
            "market_regime": regime["regime"],
            "vix_level":     regime.get("vix"),
            "alert":         "🚨 BEAR MARKET — Move to cash. No long positions.",
            "strong_buys":   [],
            "buys":          [],
            "watch_list":    [],
            "total_scanned": 0,
            "regime":        regime,
        }

    # ── Load universe ──────────────────────────────────────────────────────
    universe = get_universe(universe_name)
    log.info(f"Universe: {len(universe)} tickers from {universe_name}")

    # ── Pre-filter ────────────────────────────────────────────────────────
    if pre_filter:
        candidates = technical_pre_filter(universe)
    else:
        candidates = universe

    # Limit full analysis to top candidates (API rate limits)
    candidates = candidates[:max_signals]
    log.info(f"Running full analysis on {len(candidates)} candidates ...")

    # ── Full signal generation ─────────────────────────────────────────────
    results = []
    for ticker in candidates:
        try:
            signal = generate_signal(ticker, macro_regime=regime)

            if signal["signal"] in BUY_SIGNALS:
                plan = generate_trade_plan(
                    ticker,
                    signal,
                    account_size=account_size,
                    df=signal.get("_df"),
                )
                # Merge score into plan for sorting
                plan["composite_score"] = signal["score"]
                plan["signal"]          = signal["signal"]
                results.append(plan)

            time.sleep(0.2)  # Gentle rate limiting

        except Exception as e:
            log.error(f"{ticker}: scan error: {e}")
            continue

    # Sort by composite score descending
    results.sort(key=lambda x: x.get("composite_score", 0), reverse=True)

    strong_buys = [r for r in results if r.get("signal") == "STRONG_BUY"][:5]
    buys        = [r for r in results if r.get("signal") == "BUY"][:5]
    watch_list  = [r for r in results if r.get("signal") == "WEAK_BUY"][:5]

    elapsed = (datetime.now() - scan_start).total_seconds()
    log.info(
        f"=== Scan Complete in {elapsed:.1f}s — "
        f"{len(strong_buys)} STRONG BUY, {len(buys)} BUY, {len(watch_list)} WATCH ==="
    )

    return {
        "scan_date":     scan_start.strftime("%Y-%m-%d %H:%M"),
        "elapsed_sec":   round(elapsed, 1),
        "market_regime": regime["regime"],
        "vix_level":     regime.get("vix"),
        "strong_buys":   strong_buys,
        "buys":          buys,
        "watch_list":    watch_list,
        "total_scanned": len(candidates),
        "total_signals": len(results),
        "regime":        regime,
        "regime_advice": regime["strategy"],
    }


def scan_single_ticker(ticker: str, account_size: Optional[float] = None) -> dict:
    """
    Run full analysis and return a trade plan for a single ticker.
    Useful for ad-hoc investigation.
    """
    if account_size is None:
        account_size = config.ACCOUNT_SIZE

    regime = detect_market_regime()
    signal = generate_signal(ticker, macro_regime=regime)

    if signal["signal"] in ("ERROR", "FILTERED_OUT"):
        return signal

    plan = generate_trade_plan(
        ticker,
        signal,
        account_size=account_size,
        df=signal.get("_df"),
    )
    plan["composite_score"] = signal["score"]
    plan["scores"]          = signal.get("scores", {})
    plan["regime"]          = regime["regime"]

    return plan
