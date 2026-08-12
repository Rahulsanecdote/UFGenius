#!/usr/bin/env python3
"""
Alpaca Signal Bot — Main CLI Entry Point

Usage:
    python bot.py --mode scan                          # Run full market scan once
    python bot.py --mode scan --ticker AAPL            # Analyse a single ticker
    python bot.py --mode paper                         # Run on schedule (no live alerts)
    python bot.py --mode live                          # Run on schedule (live alerts)
    python bot.py --mode live --execute                # Live alerts + paper-account orders
    python bot.py --mode live --live-execute           # Live alerts + REAL-MONEY orders
    python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
    python bot.py --mode portfolio                     # View Alpaca portfolio (read-only)

⚠️  DISCLAIMER: This tool is for educational purposes only.
    NOT financial advice. All trading involves risk of loss.
    PAPER TRADE for ≥30 days before using real money.
"""

import argparse
import json
import re
import sys
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

from src.alerts.email_alert import send_scan_digest
from src.alerts.telegram_alert import send_telegram_alert
from src.backtest.engine import backtest_signal_system
from src.data.universe import get_universe
from src.alpaca.portfolio import get_portfolio_data
from src.scanner.daily_scan import run_daily_scan, scan_single_ticker
from src.utils import config
from src.utils.logger import get_logger

log = get_logger("bot")

# Module-level position tracker — initialised lazily when execution flags are set.
_position_tracker = None
_tracker_lock = threading.Lock()


def _get_tracker():
    """Return the module-level PositionTracker, creating and loading it on first call."""
    global _position_tracker
    if _position_tracker is not None:
        return _position_tracker
    # Double-checked locking: safe when _schedule_scan initialises the tracker
    # while the monitor thread may also call _get_tracker concurrently.
    with _tracker_lock:
        if _position_tracker is None:
            from src.alpaca.position_tracker import PositionTracker
            _position_tracker = PositionTracker()
            _position_tracker.load()
    return _position_tracker


DISCLAIMER = """
╔══════════════════════════════════════════════════════════════╗
║                    CRITICAL DISCLAIMER                        ║
╠══════════════════════════════════════════════════════════════╣
║  1. THIS IS NOT FINANCIAL ADVICE                             ║
║  2. PAPER TRADE FOR 30 DAYS MINIMUM BEFORE REAL MONEY        ║
║  3. NEVER RISK MONEY YOU CANNOT AFFORD TO LOSE               ║
║  4. SET STOP LOSSES ON EVERY SINGLE TRADE                    ║
║  5. PAST BACKTEST RESULTS ≠ FUTURE PERFORMANCE              ║
╚══════════════════════════════════════════════════════════════╝
"""


def _print_json(obj: dict) -> None:
    # Remove non-serialisable keys (DataFrames stored in _df)
    def _clean(d):
        if isinstance(d, dict):
            return {k: _clean(v) for k, v in d.items() if k != "_df"}
        if isinstance(d, list):
            return [_clean(i) for i in d]
        try:
            json.dumps(d)
            return d
        except (TypeError, ValueError):
            return str(d)

    print(json.dumps(_clean(obj), indent=2))


def _print_trade_plan(plan: dict) -> None:
    """Pretty-print a trade plan to stdout."""
    ticker = plan.get("ticker", "?")
    signal = plan.get("signal", "?")
    score  = plan.get("composite_score", 0)
    entry  = plan.get("entry", {})
    stop   = plan.get("stop_loss", {})
    targets = plan.get("targets", {})
    pos    = plan.get("position", {})
    reasons = plan.get("reasoning", [])
    risks  = plan.get("risk_factors", [])

    print(f"\n{'='*60}")
    print(f"  {signal} — {ticker}  |  Score: {score:.1f}/100")
    print(f"{'='*60}")
    print(f"  Entry:    ${entry.get('price', '?')} (LIMIT ORDER)")
    print(f"  Stop:     ${stop.get('price', '?')}  ({stop.get('pct_below_entry', '?')}% risk)")
    for label, t in targets.items():
        print(f"  {label}:      ${t.get('price', '?')}  (R:R {t.get('rr', '?')}, exit {t.get('exit_pct', '?')}%)")
    print(f"\n  Position: {pos.get('shares', '?')} shares = ${pos.get('position_value', '?')}")
    print(f"  Risk:     ${pos.get('risk_dollars', '?')} ({pos.get('risk_percent', '?')}% of account)")
    print(f"  EV/trade: ${plan.get('expected_value', '?')}")
    print("\n  Reasons:")
    for r in reasons[:6]:
        print(f"    • {r}")
    if risks:
        print("\n  Risk Factors:")
        for r in risks[:3]:
            print(f"    ⚠ {r}")
    print("\n  ⚠️  NOT FINANCIAL ADVICE. Paper trade first.")
    print(f"{'='*60}\n")


def cmd_screen(args) -> None:
    """Filter a universe with a named screener preset → a candidate watchlist.

    A screener finds *candidates*, not trades: the output feeds --mode scan /
    --mode intraday-scan (which, after validation, decide anything about money).
    """
    from src.data.universe import get_universe
    from src.screener.screener import get_preset, screen_ticker, screen_universe

    available = ", ".join(sorted((config.SCREENER_PRESETS or {}).keys())) or "(none configured)"
    if not args.preset:
        log.error("--mode screen requires --preset NAME")
        print(f"Available presets: {available}")
        sys.exit(1)
    preset = get_preset(args.preset)
    if preset is None:
        log.error(f"Unknown preset '{args.preset}'. Available: {available}")
        sys.exit(1)

    if args.ticker:  # single-name: show pass/fail + why
        res = screen_ticker(preset, args.ticker.upper())
        print(f"[{'PASS' if res.passed else 'FAIL'}] {res.ticker} — preset '{args.preset}'")
        for r in res.reasons:
            print(f"   - {r}")
        if args.json:
            _print_json(res.to_dict())
        return

    universe_name = args.universe or config.SCAN_UNIVERSE
    tickers = get_universe(universe_name)
    log.info(
        f"Screening {len(tickers)} {universe_name} tickers with '{args.preset}' "
        f"({preset.get('description', '')})"
    )
    passed = screen_universe(args.preset, tickers)
    print(f"\n{len(passed)}/{len(tickers)} match preset '{args.preset}':")
    for res in passed:
        f = res.features
        print(
            f"  {res.ticker:<6} ${f.get('price') or 0:>7.2f}  "
            f"RSI {f.get('rsi14') or 0:>5.1f}  RVOL {f.get('rel_volume') or 0:>5.2f}x"
        )
    if passed:
        wl = ",".join(r.ticker for r in passed)
        print(
            "\nFeed these into a scan by setting the custom watchlist:\n"
            f'  CUSTOM_WATCHLIST="{wl}"\n'
            "  python bot.py --mode scan --universe CUSTOM\n"
            "  python bot.py --mode intraday-scan --universe CUSTOM   # for intraday entries"
        )
    if args.json:
        _print_json([r.to_dict() for r in passed])


def cmd_scan(args) -> None:
    """Run a market scan (single ticker or full universe)."""
    account_size = args.account_size or config.ACCOUNT_SIZE

    if args.ticker:
        log.info(f"Single ticker scan: {args.ticker.upper()}")
        plan = scan_single_ticker(args.ticker.upper(), account_size=account_size)
        _print_trade_plan(plan)
        if args.json:
            _print_json(plan)
    else:
        log.info("Running full market scan ...")
        result = run_daily_scan(
            account_size=account_size,
            universe_name=args.universe or config.SCAN_UNIVERSE,
        )

        print(f"\n{'='*60}")
        print(f"  DAILY SCAN — {result.get('scan_date', '')}")
        print(f"  Regime: {result.get('market_regime', '?')}  |  VIX: {result.get('vix_level', '?')}")
        print(f"  Scanned: {result.get('total_scanned', 0)} tickers | "
              f"Signals: {result.get('total_signals', 0)}")
        print(f"{'='*60}\n")

        for category, plans in [
            ("🚀 STRONG BUY", result.get("strong_buys", [])),
            ("📈 BUY",        result.get("buys", [])),
            ("🔍 WATCH LIST", result.get("watch_list", [])),
        ]:
            if plans:
                print(f"\n{category}:")
                for plan in plans:
                    _print_trade_plan(plan)

        if args.json:
            _print_json(result)

        # Send alerts if live mode
        if args.mode == "live":
            for plan in result.get("strong_buys", []) + result.get("buys", []):
                try:
                    send_telegram_alert(plan)
                except Exception as exc:
                    log.warning(
                        f"Telegram alert failed for {plan.get('ticker', '?')}: {exc}",
                        exc_info=True,
                    )
            try:
                send_scan_digest(result)
            except Exception as exc:
                log.warning(f"Scan digest alert failed: {exc}", exc_info=True)

        # Execute trades if --execute or --live-execute flag is active
        _maybe_execute(args, result)


def _maybe_execute(args, scan_result: dict) -> None:
    """
    Submit entry orders for BUY/STRONG_BUY plans when execution flags are active.

    --execute       → place orders on the Alpaca PAPER account (ALPACA_PAPER=true)
    --live-execute  → place orders on the LIVE Alpaca account  (ALPACA_PAPER=false)
    --dry-run       → preview only; never submits an order (works with either)

    Only runs when --mode live is also set.
    """
    execute = getattr(args, "execute", False)
    live_execute = getattr(args, "live_execute", False)
    if not (execute or live_execute):
        return
    if args.mode != "live":
        return

    # Preview is now an explicit opt-in. Previously --execute forced dry_run,
    # so the ONLY path that actually submitted was --live-execute against the
    # LIVE account — the opposite of a safety rail (audit H3). Now --execute
    # submits to the paper account; --dry-run previews without submitting.
    dry_run = getattr(args, "dry_run", False)

    from src.alpaca.executor import execute_trade_plan
    tracker = _get_tracker()
    plans = scan_result.get("strong_buys", []) + scan_result.get("buys", [])

    for plan in plans:
        ticker = plan.get("ticker", "?")
        try:
            outcome = execute_trade_plan(plan, tracker, dry_run=dry_run)
            if outcome.get("dry_run"):
                log.info(
                    f"[DRY RUN] {ticker} — would submit"
                    f" {outcome['shares']} shares @ ${outcome['limit_price']:.2f}"
                )
            elif outcome["ok"]:
                log.info(
                    f"Order placed: {ticker} x{outcome['shares']}"
                    f" @ ${outcome['limit_price']:.2f}"
                    f" (order_id={outcome['order_id']})"
                )
            else:
                log.warning(f"Trade rejected [{ticker}]: {outcome['reason']}")
        except Exception as exc:
            log.error(f"Execution error [{ticker}]: {exc}", exc_info=True)


def _resolve_tickers(args, *, what: str) -> list[str]:
    """Resolve the ticker list for a backtest / validate / optimize run.

    Defaults to the **full** configured universe. Previously these three commands
    silently truncated to the first 50 tickers, which is the *alphabetical head*
    of the index — so a run reported as "the S&P 500" actually measured ~10% of
    it, skewed to A-names. That silent narrowing is the opposite of what a
    validation harness is for, so the cap is now opt-in (`--max-tickers`) and
    logged loudly whenever it bites.
    """
    if args.ticker:
        return [args.ticker.upper()]

    universe = get_universe(args.universe or config.SCAN_UNIVERSE)
    cap = getattr(args, "max_tickers", None)
    if cap and cap < len(universe):
        log.warning(
            f"{what}: capping the universe at {cap} of {len(universe)} tickers "
            "(--max-tickers). This takes the ALPHABETICAL HEAD, not a "
            "representative sample — the result describes that subsample only, "
            "not the universe."
        )
        return universe[:cap]

    log.info(f"{what}: using the full {len(universe)}-ticker universe.")
    return universe


def cmd_backtest(args) -> None:
    """Run historical backtest."""
    start = args.start or "2022-01-01"
    end   = args.end   or "2023-12-31"

    tickers = _resolve_tickers(args, what="Backtest")

    capital = args.account_size or config.ACCOUNT_SIZE

    log.info(f"Backtesting {len(tickers)} tickers from {start} to {end} ...")
    result = backtest_signal_system(tickers, start, end, initial_capital=capital)

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS: {result.get('period', '')}")
    print(f"{'='*60}")
    print(f"  Total Return:      {result.get('total_return_pct', 0):+.2f}%")
    print(f"  Annual Return:     {result.get('annual_return_pct', 0):+.2f}%")
    print(f"  Sharpe Ratio:      {result.get('sharpe_ratio', 0):.2f}")
    print(f"  Sortino Ratio:     {result.get('sortino_ratio', 0):.2f}")
    print(f"  Max Drawdown:      {result.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Calmar Ratio:      {result.get('calmar_ratio', 0):.2f}")
    print(f"  Total Trades:      {result.get('total_trades', 0)}")
    print(f"  Win Rate:          {result.get('win_rate_pct', 0):.1f}%")
    _pf = result.get("profit_factor")
    print(f"  Profit Factor:     {_pf:.2f}" if _pf is not None else "  Profit Factor:     ∞ (no losing trades)")
    print(f"  EV / Trade:        ${result.get('ev_per_trade', 0):.2f}")
    print(f"  Final Capital:     ${result.get('final_capital', 0):,.2f}")
    print("\n  Acceptance Check:")

    checks = result.get("minimum_acceptance", {})
    for k, v in checks.items():
        if k not in ("all_pass", "verdict"):
            status = "✅" if v else "❌"
            print(f"    {status} {k.replace('_ok', '').replace('_', ' ').title()}: {'PASS' if v else 'FAIL'}")
    print(f"\n  Verdict: {checks.get('verdict', 'N/A')}")
    print(f"{'='*60}\n")

    if args.json:
        _print_json(result)


def cmd_intraday_backtest(args) -> None:
    """Backtest an intraday entry (breakout / sweep-reclaim) on historical bars.

    Replays the deterministic intraday entry bar-by-bar with no look-ahead:
    next-open fills (same session only), intrabar stop/target management, and
    forced flat at session end (day-trading — no overnight holds). This is the
    out-of-sample check the intraday entries previously lacked; `--mode validate`
    only covers the daily composite.
    """
    from src.backtest.intraday_engine import backtest_intraday

    entry = args.entry or "breakout"
    interval = args.interval or config.INTRADAY_DEFAULT_INTERVAL
    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        universe = get_universe(args.universe or config.SCAN_UNIVERSE)
        cap = config.INTRADAY_BACKTEST_MAX_TICKERS
        if len(universe) > cap:
            log.info(f"Universe has {len(universe)} tickers; capping to {cap} (intraday_backtest.max_tickers)")
        tickers = universe[:cap]
    capital = args.account_size or config.ACCOUNT_SIZE

    log.info(
        f"Intraday backtest: entry={entry} interval={interval} "
        f"tickers={len(tickers)} range={args.start or 'earliest'}→{args.end or 'latest'}"
    )
    result = backtest_intraday(
        tickers, args.start, args.end,
        interval=interval, entry=entry, initial_capital=capital,
    )

    print(f"\n{'='*60}")
    print(f"  INTRADAY BACKTEST: {result.get('entry','?')} @ {result.get('interval','?')}  ({result.get('period','')})")
    print(f"{'='*60}")
    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        print(f"{'='*60}\n")
        return
    print(f"  Tickers Tested:    {result.get('tickers_tested', 0)}")
    print(f"  Total Trades:      {result.get('total_trades', 0)}")
    if not result.get("total_trades"):
        print(f"  {result.get('note','No trades.')}")
        print(f"\n  Verdict: {result.get('minimum_acceptance', {}).get('verdict', 'N/A')}")
        print(f"{'='*60}\n")
        if args.json:
            _print_json(result)
        return
    print(f"  Win Rate:          {result.get('win_rate_pct', 0):.1f}%")
    print(f"  Expectancy (R):    {result.get('expectancy_r', 0):+.3f}")
    print(f"  Avg Win / Loss (R):{result.get('avg_win_r', 0):+.2f} / {result.get('avg_loss_r', 0):+.2f}")
    _pf = result.get("profit_factor")
    print(f"  Profit Factor:     {_pf:.2f}" if _pf is not None else "  Profit Factor:     ∞ (no losing trades)")
    print(f"  Avg % / Trade:     {result.get('avg_pct_return', 0):+.3f}%")
    print(f"  Avg Hold (bars):   {result.get('avg_hold_bars', 0):.1f}")
    print(f"  Total Return:      {result.get('total_return_pct', 0):+.2f}%  (risk {result.get('cost_model',{}).get('risk_per_trade',0):.1%}/trade)")
    print(f"  Max Drawdown:      {result.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Exit Breakdown:    {result.get('exit_breakdown', {})}")
    print("\n  Acceptance Check:")
    checks = result.get("minimum_acceptance", {})
    for k, v in checks.items():
        if k not in ("all_pass", "verdict"):
            status = "✅" if v else "❌"
            print(f"    {status} {k.replace('_ok', '').replace('_', ' ').title()}: {'PASS' if v else 'FAIL'}")
    print(f"\n  Verdict: {checks.get('verdict', 'N/A')}")
    for d in result.get("bias_disclosures", []):
        print(f"    ⚠️  {d}")
    print(f"{'='*60}\n")

    if args.json:
        _print_json(result)


def cmd_validate(args) -> None:
    """Validate the strategy edge out-of-sample (upgrade plan P0.1).

    Runs walk-forward across the in-sample span, a held-out out-of-sample
    backtest, and bootstrap confidence intervals, then prints a pass/fail
    verdict. A NOT VALIDATED result means: do not deploy capital.
    """
    from src.backtest.validation import validate_strategy

    start = args.start or "2022-01-01"
    end   = args.end   or "2023-12-31"
    tickers = _resolve_tickers(args, what="Validation")
    capital = args.account_size or config.ACCOUNT_SIZE

    log.info(
        f"Validating {len(tickers)} tickers {start}→{end} "
        f"(windows={args.windows}, bootstrap={args.bootstrap}, oos={args.oos_fraction})"
    )
    result = validate_strategy(
        tickers, start, end,
        initial_capital=capital,
        n_windows=args.windows,
        n_bootstrap=args.bootstrap,
        oos_fraction=args.oos_fraction,
        seed=args.seed,
    )

    split = result["split"]
    oos = result["out_of_sample"]
    wf = result["walk_forward"]["stability"]
    boot = result["bootstrap_out_of_sample"]["return_level"].get("sharpe_ratio", {})
    trade_boot = result["bootstrap_out_of_sample"]["trade_level"]
    verdict = result["verdict"]

    print(f"\n{'='*64}")
    print("  STRATEGY EDGE VALIDATION (P0.1)")
    print(f"{'='*64}")
    print(f"  In-sample:      {split['in_sample']}")
    print(f"  Held-out OOS:   {split['out_of_sample']}")
    print("\n  Walk-forward (in-sample windows):")
    print(f"    Windows profitable:  {wf['windows_profitable']}/{wf['n_windows_with_trades']}"
          f"  (accepted: {wf['windows_accepted']})")
    print(f"    Sharpe mean/min:     {wf['sharpe_mean']} / {wf['sharpe_min']}")
    print("\n  Out-of-sample (held out):")
    print(f"    Sharpe:              {oos['sharpe_ratio']}")
    print(f"    Total return:        {oos['total_return_pct']}%")
    print(f"    Max drawdown:        {oos['max_drawdown_pct']}%")
    print(f"    Trades:              {oos['total_trades']}")
    print("\n  Bootstrap CIs (out-of-sample):")
    print(f"    Sharpe p05/p50/p95:  {boot.get('p05')} / {boot.get('p50')} / {boot.get('p95')}")
    print(f"    Prob. profitable:    {trade_boot.get('prob_profitable')}")
    print("\n  Verdict checks:")
    for k, v in verdict["checks"].items():
        status = "✅" if v else "❌"
        print(f"    {status} {k.replace('_ok', '').replace('_', ' ').title()}")
    banner = "✅ VALIDATED" if verdict["validated"] else "❌ NOT VALIDATED"
    print(f"\n  {banner}")
    print(f"  {verdict['summary']}")
    print(f"\n  ⚠️  {verdict['disclaimer']}")
    print(f"{'='*64}\n")

    if getattr(args, "save_baseline", False):
        from src.backtest.baseline import save_baseline

        saved = save_baseline(
            result,
            tickers=tickers, start=start, end=end,
            initial_capital=capital, seed=args.seed,
        )
        print(f"  Baseline saved → {config.PAPER_SCORECARD_BASELINE_PATH}")
        if not saved["validated"]:
            # Saved for the record, but the tolerance gate refuses an unvalidated
            # reference — say so here rather than letting an operator discover it
            # only when a live entry is blocked.
            print("  ⚠️  This run was NOT VALIDATED — the paper-vs-backtest gate will "
                  "refuse it as a reference until a validated run replaces it.")
        print()

    if args.json:
        _print_json(result)


def cmd_optimize(args) -> None:
    """Parameter search with anti-overfitting discipline (upgrade plan P0.2).

    Scores a coarse grid in-sample only, applies the false-strategy
    (multiple-testing) haircut, and confirms the winner on the held-out OOS
    tail. NOT TRUSTWORTHY means the best config is likely curve-fit — do not
    deploy it.
    """
    from src.backtest.optimize import DEFAULT_GRID, parameter_search

    start = args.start or "2022-01-01"
    end   = args.end   or "2023-12-31"
    tickers = _resolve_tickers(args, what="Parameter search")
    capital = args.account_size or config.ACCOUNT_SIZE

    log.info(
        f"Parameter search over {len(tickers)} tickers {start}→{end} "
        f"(grid={DEFAULT_GRID}, windows={args.windows}, oos={args.oos_fraction})"
    )
    result = parameter_search(
        tickers, start, end, DEFAULT_GRID,
        initial_capital=capital,
        n_windows=args.windows,
        n_bootstrap=args.bootstrap,
        oos_fraction=args.oos_fraction,
        seed=args.seed,
    )

    sel = result["selection"]
    print(f"\n{'='*64}")
    print("  PARAMETER SELECTION (P0.2)")
    print(f"{'='*64}")
    print(f"  In-sample:   {result['split']['in_sample']}")
    print(f"  Held-out:    {result['split']['out_of_sample']}")
    print(f"  Candidates:  {result['n_candidates']} (rankable: {result['n_rankable']})")
    print(f"  False-strategy threshold (E[max Sharpe]): {result['false_strategy_threshold']}")
    print("\n  Top in-sample candidates:")
    for i, cand in enumerate(result["ranking"][:5], 1):
        print(f"    {i}. sharpe_mean={cand['insample_sharpe_mean']}  {cand['params']}")
    if sel["params"]:
        print("\n  Selected parameters:")
        print(f"    {sel['params']}")
        print(f"    in-sample sharpe_mean:      {sel['insample_sharpe_mean']}")
        print(f"    beats false-strategy thr.:  {sel['beats_false_strategy_threshold']}")
        oos = sel["out_of_sample"]
        if oos:
            print(f"    OOS sharpe / prob-profit:   {oos['sharpe_ratio']} / {oos['prob_profitable']}")
            print(f"    OOS confirmed:              {oos['confirmed']}")
    banner = "✅ TRUSTWORTHY" if sel["trustworthy"] else "❌ NOT TRUSTWORTHY"
    print(f"\n  {banner}")
    print(f"  {sel['summary']}")
    print(f"\n  ⚠️  {result['disclaimer']}")
    print(f"{'='*64}\n")

    if args.json:
        _print_json(result)


def cmd_portfolio(args) -> None:
    """Display Alpaca portfolio (read-only)."""
    data = get_portfolio_data()

    if "error" in data:
        print(f"\n  ⚠️  {data['error']}\n")
        return

    print(f"\n{'='*60}")
    print("  ALPACA PORTFOLIO (READ-ONLY)")
    print(f"{'='*60}")
    print(f"  Total Equity:  ${data.get('total_equity', 0):,.2f}")
    print(f"  Buying Power:  ${data.get('buying_power', 0):,.2f}")
    print("\n  Holdings:")
    for h in data.get("holdings", []):
        pnl_str = f"+${h['pnl']:.2f}" if h["pnl"] >= 0 else f"-${abs(h['pnl']):.2f}"
        print(
            f"    {h['ticker']:6s}  {h['shares']:.0f} shares  "
            f"@ ${h['avg_cost']:.2f}  →  ${h['current']:.2f}  "
            f"({pnl_str} / {h['pnl_pct']:+.1f}%)"
        )
    print(f"\n  ⚠️  {data.get('note', '')}")
    print(f"{'='*60}\n")

    _print_paper_scorecard()
    _print_execution_quality()


def _print_execution_quality() -> None:
    """Print the P2.1 execution-quality summary from the local fill ledger."""
    try:
        from src.alpaca.execution_quality import ExecutionQualityLedger

        summary = ExecutionQualityLedger().load().summary()
    except Exception as exc:
        log.debug(f"execution-quality summary unavailable: {exc}")
        return

    print(f"{'='*60}")
    print("  EXECUTION QUALITY (P2.1)")
    print(f"{'='*60}")
    if summary.get("n_fills", 0) == 0:
        print("  No fills recorded yet.")
        print(f"{'='*60}\n")
        return
    print(f"  Fills recorded:       {summary['n_fills']}")
    print(f"  Avg slippage:         {summary['avg_slippage_bps']} bps "
          f"(entry {summary['avg_entry_slippage_bps']} / exit {summary['avg_exit_slippage_bps']})")
    print(f"  Implementation short.: ${summary['total_implementation_shortfall']}")
    measured = summary.get("measured_slippage_pct")
    print(f"  Measured slippage:    {measured if measured is not None else 'n/a (too few fills)'}")
    print(f"{'='*60}\n")


def _print_paper_scorecard() -> None:
    """Print the P0.4 paper-trading scorecard from the local trade ledger."""
    try:
        from src.alpaca.position_tracker import PositionTracker
        from src.alpaca.scorecard import scorecard_from_tracker

        tracker = PositionTracker()
        tracker.load()
        card = scorecard_from_tracker(tracker, initial_capital=config.ACCOUNT_SIZE)
    except Exception as exc:  # never let the scorecard break the portfolio view
        log.debug(f"scorecard unavailable: {exc}")
        return

    print(f"{'='*60}")
    print("  PAPER-TRADING SCORECARD (P0.4)")
    print(f"{'='*60}")
    if card.get("n_trades", 0) == 0:
        print(f"  {card.get('summary', 'No closed paper trades yet.')}")
        print(f"{'='*60}\n")
        return
    acc = card.get("acceptance", {})
    print(f"  Closed trades:      {card['n_trades']}  "
          f"(W {card['wins']} / L {card['losses']}, win rate {card['win_rate_pct']}%)")
    print(f"  Profit factor:      {card['profit_factor']}")
    print(f"  Expectancy/trade:   ${card['expectancy_per_trade']}")
    print(f"  Total realized P&L: ${card['total_pnl']}  ({card['total_return_pct']}%)")
    print(f"  Prob. profitable:   {card['prob_profitable']} (bootstrap)")
    banner = "✅ MEETS live-performance floors" if acc.get("all_pass") else "❌ BELOW floors"
    print(f"\n  {banner}")
    print(f"  {card.get('summary', '')}")

    try:  # the baseline comparison must never break the portfolio view either
        from src.backtest.baseline import compare_paper_to_baseline, load_baseline

        baseline = load_baseline()
        if baseline is not None:
            cmp_ = compare_paper_to_baseline(card, baseline)
            gate_on = config.PAPER_SCORECARD_BASELINE_GATE_ENABLED
            cmp_banner = "✅ WITHIN tolerance" if cmp_.get("all_pass") else "❌ OUTSIDE tolerance"
            print("\n  vs validated backtest "
                  f"({baseline.get('provenance', {}).get('out_of_sample')}):")
            for metric, chk in (cmp_.get("checks") or {}).items():
                if not chk.get("comparable"):
                    continue
                mark = "✅" if chk.get("pass") else "❌"
                print(f"    {mark} {metric}: paper {chk.get('paper')} "
                      f"vs backtest {chk.get('baseline')} (floor {chk.get('floor')})")
            print(f"  {cmp_banner}"
                  f"{'' if gate_on else '  (advisory — baseline_gate_enabled=false)'}")
            print(f"  {cmp_.get('reason', '')}")
            if cmp_.get("divergence_note"):
                print(f"  ⚠️  {cmp_['divergence_note']}")
    except Exception as exc:
        log.debug(f"baseline comparison unavailable: {exc}")
    print(f"  ⚠️  {card.get('disclaimer', '')}")
    print(f"{'='*60}\n")


def cmd_intraday_scan(args) -> None:
    """Run the continuous intraday scan + entry pipeline (upgrade plan P1.2/P1.3).

    Producer (P1.2): fans the unusual-volume / momentum / breakout / gap scanners
    over live intraday bars on a short interval into a deduped queue.
    Consumer (P1.3): drains the queue, runs the deterministic intraday entry
    evaluator (VWAP reclaim + opening-range breakout + volume confirmation) and
    logs the resulting intraday trade plan (intraday-ATR stop) for each entry.

    Discovery + planning only — it logs entry plans, it does not place orders.
    The consumer's sink is pluggable, so routing plans through the existing
    RiskGuard/executor path is a one-line change once the edge is validated.
    """
    from src.data.universe import get_universe
    from src.scanner.candidate_queue import CandidateQueue
    from src.scanner.intraday_consumer import IntradayConsumer
    from src.scanner.intraday_scan import ContinuousScanner

    universe_name = args.universe or config.SCAN_UNIVERSE
    tickers = [args.ticker.upper()] if args.ticker else get_universe(universe_name)
    account_size = args.account_size or config.ACCOUNT_SIZE

    queue = CandidateQueue(
        maxlen=int(config.CONTINUOUS_SCAN_QUEUE_MAX),
        dedup_ttl_sec=float(config.CONTINUOUS_SCAN_DEDUP_TTL_SEC),
    )
    scanner = ContinuousScanner(tickers, queue=queue)
    consumer = IntradayConsumer(queue, account_size=account_size)

    print(f"\n{'='*60}")
    print("  CONTINUOUS INTRADAY SCAN + ENTRY (P1.2 / P1.3)")
    print(f"{'='*60}")
    print(f"  Universe:  {len(scanner.universe)} tickers ({universe_name}), <= {scanner._cap}/cycle")
    print(f"  Interval:  every {scanner.interval_sec}s on {config.CONTINUOUS_SCAN_INTERVAL} bars")
    print("  Producer scans → queue → consumer evaluates VWAP/ORB/volume entries.")
    print("  Logs intraday entry plans (discovery only; no orders placed).")
    print("  Scan window (incl. pre-market) only. Press Ctrl+C to stop.")
    print(f"{'='*60}\n")

    try:
        scanner.run_forever(on_cycle=consumer.drain_once)
    except KeyboardInterrupt:
        scanner.stop()
        print("\n  Scanner stopped.\n")


def cmd_earnings_calendar(args) -> None:
    """Build/refresh the P1.4 earnings calendar from the data provider.

    Populates ``catalysts.earnings_calendar_path`` with next-earnings dates for
    the universe so the RiskGuard earnings-week block is calendar-backed rather
    than doing a per-ticker lookup at decision time.
    """
    from src.catalysts.earnings_calendar import EarningsCalendar
    from src.data.universe import get_universe

    universe_name = args.universe or config.SCAN_UNIVERSE
    tickers = [args.ticker.upper()] if args.ticker else get_universe(universe_name)

    print(f"\n{'='*60}")
    print("  EARNINGS CALENDAR REFRESH (P1.4)")
    print(f"{'='*60}")
    print(f"  Universe:  {len(tickers)} tickers ({universe_name})")
    print(f"  Path:      {config.CATALYST_EARNINGS_CALENDAR_PATH}")
    print("  Fetching next-earnings dates (best-effort per ticker) ...")

    cal = EarningsCalendar()
    written = cal.refresh_from_provider(tickers)
    print(f"\n  Wrote {written}/{len(tickers)} earnings dates.")
    print(f"{'='*60}\n")


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

_DEFAULT_SCHEDULE = {
    "pre_market": "06:00",
    "market_open": "09:25",
    "post_market": "16:30",
    "overnight": "21:00",
}


def _is_trading_day(now: datetime | None = None) -> bool:
    """Weekend gate (audit L4): skip Sat/Sun instead of scanning a closed
    market. The weekday is derived in America/New_York (the market timezone
    the executor's monitor already anchors to) so a host in another timezone
    doesn't misclassify the US trading date — e.g. a Monday pre-dawn run in
    Tokyo is still Sunday in New York. US market holidays still slip through —
    a proper trading calendar is a heavier dependency than this gate warrants
    today. `now` may be passed for testing; otherwise the current market time
    is used."""
    if now is None:
        try:
            now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now = datetime.now()  # fail-open to local time on tz errors
    return now.weekday() < 5


def _wire_schedule(sched: dict, run_fn) -> list[str]:
    """Wire EVERY configured `schedule:` slot (audit M12) — previously only
    four hardcoded slots were scheduled and intraday_1/intraday_2 were
    silently dropped. Returns the wired "slot=HH:MM" entries."""
    if not sched:
        sched = dict(_DEFAULT_SCHEDULE)
    wired: list[str] = []
    for slot, time_str in sorted(sched.items(), key=lambda kv: str(kv[1])):
        if not isinstance(time_str, str) or not _TIME_RE.match(time_str):
            log.error(
                f"Invalid schedule time {time_str!r} for '{slot}' "
                "(expected HH:MM 24h); skipping"
            )
            continue
        schedule.every().day.at(time_str).do(run_fn)
        wired.append(f"{slot}={time_str}")
    return wired


def _schedule_scan(args) -> None:
    """Run scan on schedule."""
    sched = config.get("schedule", {})

    # Start position monitor thread if execution flags are set
    if getattr(args, "execute", False) or getattr(args, "live_execute", False):
        from src.alpaca.executor import start_monitor_thread
        tracker = _get_tracker()
        start_monitor_thread(tracker)

    def _run():
        if not _is_trading_day():
            log.info("Skipping scheduled scan: weekend (market closed)")
            return
        log.info(f"Scheduled scan triggered at {datetime.now().strftime('%H:%M')}")
        cmd_scan(args)

    wired = _wire_schedule(sched, _run)
    log.info(f"Scheduled scans (weekdays): {', '.join(wired) or 'none'}")
    log.info(f"Running in {'PAPER' if args.mode == 'paper' else 'LIVE'} mode. Press Ctrl+C to stop.")

    # Run immediately on startup
    _run()

    while True:
        schedule.run_pending()
        time.sleep(30)


def _positive_account_size(value: str) -> float:
    """argparse type for --account-size: a negative/zero/NaN size silently
    poisoned position sizing downstream (audit L3) — reject it at the CLI."""
    try:
        size = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if not (size > 0) or size != size or size == float("inf"):
        raise argparse.ArgumentTypeError("account size must be a positive, finite number")
    return size


def _positive_int(value: str) -> int:
    """argparse type for --windows / --bootstrap: reject non-positive counts at
    the CLI (a negative count blows up np.empty deep in the harness)."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _oos_fraction(value: str) -> float:
    """argparse type for --oos-fraction: must leave usable data on both sides."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if not 0.05 <= parsed <= 0.9:
        raise argparse.ArgumentTypeError("must be between 0.05 and 0.9")
    return parsed


def main() -> None:
    print(DISCLAIMER)

    parser = argparse.ArgumentParser(
        description="Alpaca Signal Bot — Educational Stock Analysis Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  scan      Run a market scan once (add --ticker for single stock)
  paper     Run on schedule, log signals only (no live alerts)
  live      Run on schedule with Telegram/email alerts
  backtest  Historical simulation
  validate  Walk-forward + held-out OOS + bootstrap edge check (P0.1)
  optimize  Parameter search scored in-sample only, OOS-confirmed (P0.2)
  portfolio View Alpaca portfolio (read-only)

Examples:
  python bot.py --mode scan --ticker AAPL
  python bot.py --mode scan --account-size 25000
  python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
  python bot.py --mode intraday-backtest --entry sweep_reclaim --interval 5m  # OOS check for the intraday entries
  python bot.py --mode validate --start 2022-01-01 --end 2023-12-31
  python bot.py --mode optimize --start 2022-01-01 --end 2023-12-31
  python bot.py --mode intraday-scan                 # Continuous intraday candidate scan (P1.2)
  python bot.py --mode earnings-calendar             # Build/refresh the earnings calendar (P1.4)
  python bot.py --mode paper
        """,
    )

    parser.add_argument(
        "--mode", choices=["scan", "screen", "paper", "live", "backtest", "intraday-backtest", "validate", "optimize", "portfolio", "intraday-scan", "earnings-calendar"],
        default="scan", help="Operating mode (default: scan)",
    )
    parser.add_argument("--ticker",       help="Single ticker to analyse")
    parser.add_argument(
        "--account-size", type=_positive_account_size,
        help="Portfolio size in USD (positive number)",
    )
    parser.add_argument("--universe",     choices=["SP500", "RUSSELL1000", "CUSTOM", "WATCHLIST"], help="Ticker universe (CUSTOM/WATCHLIST read the custom watchlist)")
    parser.add_argument("--preset",       help="Screener preset name (for --mode screen), e.g. oversold-bounce")
    parser.add_argument("--entry",        choices=["breakout", "sweep_reclaim"], help="Intraday entry to backtest (for --mode intraday-backtest)")
    parser.add_argument("--interval",     help="Intraday bar size for --mode intraday-backtest (e.g. 5m, 1m; default INTRADAY_DEFAULT_INTERVAL)")
    parser.add_argument("--start",        help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end",          help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--json",         action="store_true", help="Also output raw JSON")
    # --mode validate (upgrade plan P0.1): walk-forward + held-out OOS + bootstrap
    from src.backtest.validation import DEFAULT_SEED
    parser.add_argument("--windows",      type=_positive_int, default=4,
                        help="validate: number of walk-forward windows (default 4)")
    parser.add_argument("--bootstrap",    type=_positive_int, default=1000,
                        help="validate: bootstrap resamples for CIs (default 1000)")
    parser.add_argument("--oos-fraction", type=_oos_fraction, default=0.30, dest="oos_fraction",
                        help="validate: held-out out-of-sample fraction 0.05-0.9 (default 0.30)")
    parser.add_argument("--seed",         type=int, default=DEFAULT_SEED,
                        help=f"validate: RNG seed for reproducible bootstrap (default {DEFAULT_SEED})")
    parser.add_argument("--max-tickers", type=_positive_int, default=None, dest="max_tickers",
                        help=("backtest/validate/optimize: cap the universe at N tickers. "
                              "Default is the FULL universe. Takes the alphabetical head, so "
                              "a capped run is a subsample — use it for quick checks, not for "
                              "a result you intend to report as the whole index."))
    parser.add_argument("--save-baseline", action="store_true", dest="save_baseline",
                        help=("validate: persist this run's out-of-sample metrics as the "
                              "reference the live paper-vs-backtest tolerance gate compares "
                              "against (see paper_scorecard.baseline_* in config.yaml)"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Submit entry orders on the Alpaca PAPER account after each scan "
            "(requires --mode live and ALPACA_PAPER=true in .env)"
        ),
    )
    parser.add_argument(
        "--live-execute",
        action="store_true",
        dest="live_execute",
        help=(
            "Submit entry orders on the LIVE Alpaca account after each scan "
            "(requires --mode live and ALPACA_PAPER=false in .env). "
            "⚠️  REAL MONEY — use with extreme caution."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview orders only — never submit (use with --execute/--live-execute).",
    )

    args = parser.parse_args()

    if args.account_size is not None and args.account_size <= 0:
        log.error("--account-size must be a positive number.")
        sys.exit(1)

    # --execute (paper) and --live-execute (real money) are mutually exclusive —
    # never let the paper flag silently ride along into a live-trading run.
    if args.execute and args.live_execute:
        log.error("--execute and --live-execute are mutually exclusive.")
        sys.exit(1)

    # --execute targets the PAPER account: refuse if the account is live.
    if args.execute and not args.live_execute and not config.ALPACA_PAPER:
        log.error(
            "--execute submits to the Alpaca PAPER account but ALPACA_PAPER=false. "
            "Set ALPACA_PAPER=true, or use --live-execute for real-money orders."
        )
        sys.exit(1)

    # Validate execution flag constraints
    if args.live_execute and config.ALPACA_PAPER:
        log.error(
            "--live-execute requires ALPACA_PAPER=false in your .env. "
            "Refusing to proceed: live flag set but account is still paper."
        )
        sys.exit(1)
    if args.live_execute and args.mode != "live":
        log.error("--live-execute requires --mode live.")
        sys.exit(1)
    if args.execute and args.mode != "live":
        log.error("--execute requires --mode live.")
        sys.exit(1)
    if args.live_execute:
        log.warning(
            "⚠️  LIVE TRADING MODE — real money at risk. "
            "All safety rules apply. Press Ctrl+C within 5 s to abort."
        )
        time.sleep(5)

    if args.mode in ("scan",):
        cmd_scan(args)
    elif args.mode in ("paper", "live"):
        _schedule_scan(args)
    elif args.mode == "backtest":
        cmd_backtest(args)
    elif args.mode == "intraday-backtest":
        cmd_intraday_backtest(args)
    elif args.mode == "validate":
        cmd_validate(args)
    elif args.mode == "optimize":
        cmd_optimize(args)
    elif args.mode == "portfolio":
        cmd_portfolio(args)
    elif args.mode == "screen":
        cmd_screen(args)
    elif args.mode == "intraday-scan":
        cmd_intraday_scan(args)
    elif args.mode == "earnings-calendar":
        cmd_earnings_calendar(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
