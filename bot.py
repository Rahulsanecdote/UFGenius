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


def cmd_validate(args) -> None:
    """Validate the strategy edge out-of-sample (upgrade plan P0.1).

    Runs walk-forward across the in-sample span, a held-out out-of-sample
    backtest, and bootstrap confidence intervals, then prints a pass/fail
    verdict. A NOT VALIDATED result means: do not deploy capital.
    """
    from src.backtest.validation import validate_strategy

    start = args.start or "2022-01-01"
    end   = args.end   or "2023-12-31"
    tickers = (
        [args.ticker.upper()] if args.ticker
        else get_universe(args.universe or "SP500")[:50]
    )
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
        "--mode", choices=["scan", "paper", "live", "backtest", "validate", "portfolio"],
        default="scan", help="Operating mode (default: scan)",
    )
    parser.add_argument("--ticker",       help="Single ticker to analyse")
    parser.add_argument(
        "--account-size", type=_positive_account_size,
        help="Portfolio size in USD (positive number)",
    )
    parser.add_argument("--universe",     choices=["SP500", "RUSSELL1000"], help="Ticker universe")
    parser.add_argument("--start",        help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end",          help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--json",         action="store_true", help="Also output raw JSON")
    # --mode validate (upgrade plan P0.1): walk-forward + held-out OOS + bootstrap
    parser.add_argument("--windows",      type=int, default=4,
                        help="validate: number of walk-forward windows (default 4)")
    parser.add_argument("--bootstrap",    type=int, default=1000,
                        help="validate: bootstrap resamples for CIs (default 1000)")
    parser.add_argument("--oos-fraction", type=float, default=0.30, dest="oos_fraction",
                        help="validate: held-out out-of-sample fraction (default 0.30)")
    parser.add_argument("--seed",         type=int, default=12345,
                        help="validate: RNG seed for reproducible bootstrap (default 12345)")
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
    elif args.mode == "validate":
        cmd_validate(args)
    elif args.mode == "portfolio":
        cmd_portfolio(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
