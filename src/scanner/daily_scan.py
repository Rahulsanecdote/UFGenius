"""
Daily scanner — universe filter → technical pre-filter → full signal generation.

Step 1: Load universe (S&P 500 / Russell 1000)
Step 2: Technical pre-filter (RSI, RVOL, proximity to breakout) — parallel fast pass
Step 3: Full signal generation on pre-filtered candidates — parallel
Step 4: Sort by composite score and group into strong_buys / buys / watch_list
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from src.data.fetcher import fetch_ohlcv, fetch_ohlcv_batch
from src.data.universe import get_universe
from src.macro.regime import detect_market_regime
from src.signals.generator import generate_signal
from src.signals.trade_plan import generate_trade_plan
from src.technical.momentum import calculate_momentum_indicators
from src.technical.volume import calculate_volume_indicators
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

BUY_SIGNALS      = {"STRONG_BUY", "BUY", "WEAK_BUY"}
_PREFILTER_WORKERS = 8   # threads for pre-filter stage
_SIGNAL_WORKERS    = 4   # threads for full signal generation (heavier API load)


def _prefilter_ticker(ticker: str, df_cache: dict) -> Optional[str]:
    """
    Evaluate a single ticker for the technical pre-filter.
    Returns the ticker string if it passes, None otherwise.
    """
    try:
        df = df_cache.get(ticker)
        if df is None:
            df = fetch_ohlcv(ticker, period="3mo")
        if df is None or df.empty or len(df) < 50:
            return None

        mom = calculate_momentum_indicators(df)
        vol = calculate_volume_indicators(df)

        rsi  = mom.get("RSI_14")
        rvol = vol.get("RVOL")

        rsi_val  = float(rsi.iloc[-1])  if rsi  is not None and len(rsi)  > 0 else 50.0
        rvol_val = float(rvol.iloc[-1]) if rvol is not None and len(rvol) > 0 else 1.0

        # Skip NaN
        if rsi_val != rsi_val or rvol_val != rvol_val:
            return None

        if 35 <= rsi_val <= 72 and rvol_val >= 1.3:
            return ticker

    except Exception as e:
        log.debug(f"{ticker}: pre-filter error: {e}")

    return None


def technical_pre_filter(tickers: list) -> list:
    """
    Fast parallel technical pre-filter to reduce the universe.

    Passes a ticker if ALL of:
    - RSI_14 between 35 and 72 (not extreme)
    - RVOL >= 1.3 (above-average volume interest)
    - Enough history (>50 bars)
    """
    log.info(f"Pre-filtering {len(tickers)} tickers in parallel ...")

    # Batch-fetch all OHLCV data up front (parallel, cache-aware)
    df_cache = fetch_ohlcv_batch(tickers, period="3mo", max_workers=_PREFILTER_WORKERS)

    passed = []
    with ThreadPoolExecutor(max_workers=_PREFILTER_WORKERS) as executor:
        futures = {
            executor.submit(_prefilter_ticker, t, df_cache): t
            for t in tickers
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                passed.append(result)

    # Preserve original ordering
    order = {t: i for i, t in enumerate(tickers)}
    passed.sort(key=lambda t: order.get(t, 9999))

    log.info(f"Pre-filter: {len(tickers)} → {len(passed)} candidates")
    return passed


def _analyze_ticker(ticker: str, regime: dict, account_size: float) -> Optional[dict]:
    """Run full signal + trade plan for one ticker. Returns plan dict or None."""
    try:
        signal = generate_signal(ticker, macro_regime=regime)

        if signal["signal"] not in BUY_SIGNALS:
            return None

        plan = generate_trade_plan(
            ticker,
            signal,
            account_size=account_size,
            df=signal.get("_df"),
        )
        plan["composite_score"] = signal["score"]
        plan["signal"]          = signal["signal"]
        return plan

    except Exception as e:
        log.error(f"{ticker}: scan error: {e}")
        return None


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
            "alert":         "BEAR MARKET — Move to cash. No long positions.",
            "strong_buys":   [],
            "buys":          [],
            "watch_list":    [],
            "total_scanned": 0,
            "regime":        regime,
        }

    # ── Load universe ──────────────────────────────────────────────────────
    universe = get_universe(universe_name)
    log.info(f"Universe: {len(universe)} tickers from {universe_name}")

    # ── Pre-filter (parallel) ─────────────────────────────────────────────
    if pre_filter:
        candidates = technical_pre_filter(universe)
    else:
        candidates = universe

    # Limit full analysis to top candidates (API rate limits)
    candidates = candidates[:max_signals]
    log.info(f"Running full analysis on {len(candidates)} candidates ...")

    # ── Full signal generation (parallel) ──────────────────────────────────
    results = []
    with ThreadPoolExecutor(max_workers=_SIGNAL_WORKERS) as executor:
        futures = {
            executor.submit(_analyze_ticker, t, regime, account_size): t
            for t in candidates
        }
        for future in as_completed(futures):
            plan = future.result()
            if plan is not None:
                results.append(plan)

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
