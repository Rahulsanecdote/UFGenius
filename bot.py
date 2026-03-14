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

    Only runs when --mode live is also set.
    """
    execute = getattr(args, "execute", False)
    live_execute = getattr(args, "live_execute", False)
    if not (execute or live_execute):
        return
    if args.mode != "live":
        return

    dry_run = not live_execute  # --execute = dry_run against paper account preview;
                                # --live-execute = real orders (paper or live per config)

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


def cmd_backtest(args) -> None:
    """Run historical backtest."""
    start = args.start or "2022-01-01"
    end   = args.end   or "2023-12-31"

    tickers = (
        [args.ticker.upper()] if args.ticker
        else get_universe(args.universe or "SP500")[:50]
    )

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
    print(f"  Profit Factor:     {result.get('profit_factor', 0):.2f}")
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


def _schedule_scan(args) -> None:
    """Run scan on schedule."""
    sched = config.get("schedule", {})

    # Start position monitor thread if execution flags are set
    if getattr(args, "execute", False) or getattr(args, "live_execute", False):
        from src.alpaca.executor import start_monitor_thread
        tracker = _get_tracker()
        start_monitor_thread(tracker)

    def _run():
        log.info(f"Scheduled scan triggered at {datetime.now().strftime('%H:%M')}")
        cmd_scan(args)

    _TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    for time_str in [
        sched.get("pre_market",  "06:00"),
        sched.get("market_open", "09:25"),
        sched.get("post_market", "16:30"),
        sched.get("overnight",   "21:00"),
    ]:
        if not _TIME_RE.match(time_str):
            log.error(f"Invalid schedule time '{time_str}' (expected HH:MM 24h); skipping")
            continue
        schedule.every().day.at(time_str).do(_run)

    log.info(f"Scheduled scans at: {', '.join([sched.get(k, '') for k in ['pre_market','market_open','post_market','overnight']])}")
    log.info(f"Running in {'PAPER' if args.mode == 'paper' else 'LIVE'} mode. Press Ctrl+C to stop.")

    # Run immediately on startup
    _run()

    while True:
        schedule.run_pending()
        time.sleep(30)


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
  portfolio View Alpaca portfolio (read-only)

Examples:
  python bot.py --mode scan --ticker AAPL
  python bot.py --mode scan --account-size 25000
  python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
  python bot.py --mode paper
        """,
    )

    parser.add_argument(
        "--mode", choices=["scan", "paper", "live", "backtest", "portfolio"],
        default="scan", help="Operating mode (default: scan)",
    )
    parser.add_argument("--ticker",       help="Single ticker to analyse")
    parser.add_argument("--account-size", type=float, help="Portfolio size in USD")
    parser.add_argument("--universe",     choices=["SP500", "RUSSELL1000"], help="Ticker universe")
    parser.add_argument("--start",        help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end",          help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--json",         action="store_true", help="Also output raw JSON")
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

    args = parser.parse_args()

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
    elif args.mode == "portfolio":
        cmd_portfolio(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
