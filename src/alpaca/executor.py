"""
Trade execution orchestrator.

Wires the signal pipeline → RiskGuard → Alpaca order placement → position tracking.
Also manages the background monitor thread that polls for fills and
executes partial exits at T1 / T2 / T3 targets.

Usage (from bot.py):
    from src.alpaca.executor import execute_trade_plan, start_monitor_thread
    from src.alpaca.position_tracker import PositionTracker

    tracker = PositionTracker()
    tracker.load()
    start_monitor_thread(tracker)

    outcome = execute_trade_plan(plan, tracker)
    if outcome["ok"]:
        log.info(f"Order placed: {outcome['order_id']}")
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from src.alpaca.orders import (
    OrderError,
    cancel_order,
    get_order,
    place_entry_order,
    place_limit_sell,
    place_stop_order,
)
from src.alpaca.portfolio import get_portfolio_data
from src.alpaca.position_tracker import PositionTracker
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_EXECUTABLE_SIGNALS = frozenset({"BUY", "STRONG_BUY"})


# ─── Risk Guard ───────────────────────────────────────────────────────────── #


class RiskGuard:
    """
    Enforces all SAFETY config rules before any entry order is placed.

    All checks are performed in priority order; the first failure short-circuits
    and returns an explanatory reason string.
    """

    def check(
        self,
        plan: dict,
        portfolio: dict,
        tracker: PositionTracker,
    ) -> tuple[bool, str]:
        """
        Validate a trade plan against current portfolio state and risk limits.

        Returns:
            (True, "")           — approved, proceed with execution.
            (False, reason_str)  — rejected; reason_str explains why.
        """
        safety = config.SAFETY
        signal = plan.get("signal", "")
        ticker = plan.get("ticker", "")
        position_info = plan.get("position", {})

        # 1. Signal must be actionable
        if signal not in _EXECUTABLE_SIGNALS:
            return False, f"Signal {signal!r} is not executable (only BUY / STRONG_BUY)"

        # 2. No duplicate position for same ticker
        if tracker.get(ticker) is not None:
            return False, f"Position already tracked for {ticker}"

        # 3. Portfolio must be readable
        if "error" in portfolio:
            return False, f"Portfolio unavailable: {portfolio['error']}"

        equity = float(portfolio.get("total_equity", 0))
        buying_power = float(portfolio.get("buying_power", 0))
        position_count = int(portfolio.get("position_count", 0))
        position_value = float(position_info.get("position_value", 0))
        risk_dollars = float(position_info.get("risk_dollars", 0))

        if equity <= 0:
            return False, "Account equity is zero or unavailable"

        # 4. Max concurrent open positions
        max_positions = int(safety.get("max_positions", 5))
        if position_count >= max_positions:
            return (
                False,
                f"Max open positions reached ({position_count}/{max_positions})",
            )

        # 5. Single-position size cap
        max_single_pct = float(safety.get("max_single_position_pct", 10.0))
        max_single_value = equity * (max_single_pct / 100)
        if position_value > max_single_value:
            return (
                False,
                f"Position ${position_value:.0f} exceeds {max_single_pct}% of equity"
                f" (max ${max_single_value:.0f})",
            )

        # 6. Cash reserve floor — must keep cash_reserve_pct of equity liquid
        cash_reserve_pct = float(safety.get("cash_reserve_pct", 20.0))
        required_cash = position_value + equity * (cash_reserve_pct / 100)
        if buying_power < required_cash:
            return (
                False,
                f"Insufficient buying power: ${buying_power:.0f} <"
                f" ${required_cash:.0f} (position + {cash_reserve_pct}% reserve)",
            )

        # 7. Per-trade risk dollar limit (portfolio risk budget ÷ max positions)
        max_portfolio_risk_pct = float(safety.get("max_portfolio_risk_pct", 5.0))
        max_risk_budget = equity * (max_portfolio_risk_pct / 100)
        per_trade_risk_limit = max_risk_budget / max(max_positions, 1)
        if risk_dollars > per_trade_risk_limit:
            return (
                False,
                f"Trade risk ${risk_dollars:.0f} exceeds per-trade limit"
                f" ${per_trade_risk_limit:.0f}",
            )

        # 8. Daily new-position count
        max_trades = int(safety.get("max_trades_per_day", 3))
        today_count = tracker.trades_today()
        if today_count >= max_trades:
            return False, f"Daily trade limit reached ({today_count}/{max_trades})"

        # 9. Bear market guard
        if not bool(safety.get("trade_in_bear_market", False)):
            reasons = plan.get("reasoning", [])
            if any("BEAR" in str(r).upper() for r in reasons):
                return False, "Bear market regime detected; trade_in_bear_market=False"

        return True, ""


# ─── Execution ────────────────────────────────────────────────────────────── #


def execute_trade_plan(
    plan: dict,
    tracker: PositionTracker,
    dry_run: bool = False,
) -> dict:
    """
    Run risk checks and, if approved, submit a limit entry order.

    Args:
        plan:     Trade plan dict from generate_trade_plan().
        tracker:  Live position tracker instance.
        dry_run:  If True, log intent but place no orders.  Use this when
                  ALPACA_PAPER=true to preview execution without touching
                  the paper account.

    Returns:
        {
          "ok":          bool,
          "reason":      str,          # empty on success
          "ticker":      str,
          "order_id":    str | None,   # None on dry-run or failure
          "shares":      int | None,
          "limit_price": float | None,
          "dry_run":     bool,         # True only in dry-run mode
        }
    """
    ticker = plan.get("ticker", "?")
    entry = plan.get("entry", {})
    entry_price = entry.get("price")
    shares = plan.get("position", {}).get("shares", 0)

    if not ticker or not entry_price or not shares:
        return {
            "ok": False,
            "reason": "Incomplete trade plan (missing ticker / entry price / shares)",
            "ticker": ticker,
            "order_id": None,
            "shares": None,
            "limit_price": None,
            "dry_run": dry_run,
        }

    portfolio = get_portfolio_data()
    ok, reason = RiskGuard().check(plan, portfolio, tracker)
    if not ok:
        log.warning(f"[{ticker}] Trade rejected by RiskGuard: {reason}")
        return {
            "ok": False,
            "reason": reason,
            "ticker": ticker,
            "order_id": None,
            "shares": int(shares),
            "limit_price": float(entry_price),
            "dry_run": dry_run,
        }

    if dry_run:
        log.info(
            f"[DRY RUN] Would place: {ticker} x{shares} LIMIT @ ${float(entry_price):.2f}"
            f" | stop={plan.get('stop_loss', {}).get('price')}"
        )
        return {
            "ok": True,
            "reason": "",
            "ticker": ticker,
            "order_id": None,
            "shares": int(shares),
            "limit_price": float(entry_price),
            "dry_run": True,
        }

    try:
        order = place_entry_order(ticker, int(shares), float(entry_price))
        order_id = str(order.id)
        tracker.add_position(plan, order_id)
        log.info(f"Execution confirmed: {ticker} order_id={order_id}")
        return {
            "ok": True,
            "reason": "",
            "ticker": ticker,
            "order_id": order_id,
            "shares": int(shares),
            "limit_price": float(entry_price),
            "dry_run": False,
        }
    except OrderError as exc:
        log.error(f"[{ticker}] Order placement failed: {exc}", exc_info=True)
        return {
            "ok": False,
            "reason": str(exc),
            "ticker": ticker,
            "order_id": None,
            "shares": int(shares),
            "limit_price": float(entry_price),
            "dry_run": False,
        }


# ─── Position Monitor ─────────────────────────────────────────────────────── #


def monitor_positions(tracker: PositionTracker) -> None:
    """
    Poll Alpaca order statuses and drive partial exit lifecycle.

    Called on a schedule (every MONITOR_INTERVAL_MIN minutes during market hours).

    For each pending_fill position:
      - If entry order filled → transition to active, place stop + T1/T2/T3 orders.
      - If entry order expired/cancelled → mark closed.

    For each active position:
      - If stop filled → cancel remaining target orders → mark closed.
      - If any target filled → mark hit, reduce shares_open.
      - If all shares gone → mark closed.
    """
    open_positions = tracker.get_open()
    if not open_positions:
        return

    log.debug(f"Monitor cycle: checking {len(open_positions)} open position(s)")

    for ticker, pos in list(open_positions.items()):
        try:
            if pos.status == "pending_fill":
                _check_entry_fill(ticker, pos, tracker)
            elif pos.status == "active":
                _check_exits(ticker, pos, tracker)
        except Exception as exc:
            log.error(f"Monitor error for {ticker}: {exc}", exc_info=True)


def _check_entry_fill(ticker: str, pos, tracker: PositionTracker) -> None:
    """Check if the entry order filled; if so, place stop + target orders."""
    try:
        order = get_order(pos.entry_order_id)
    except OrderError as exc:
        log.warning(f"{ticker}: could not fetch entry order: {exc}")
        return

    status = str(order.status).lower()
    log.debug(f"{ticker}: entry order status={status}")

    if status in ("filled", "partially_filled"):
        fill_price = float(order.filled_avg_price or pos.entry_price)
        filled_qty = int(float(order.filled_qty or pos.shares_initial))
        tracker.mark_entry_filled(ticker, fill_price, filled_qty)
        pos = tracker.get(ticker)  # Reload after mutation

        # Place stop-loss for the full initial position
        try:
            stop_order = place_stop_order(ticker, pos.shares_initial, pos.stop_price)
            tracker.mark_stop_placed(ticker, str(stop_order.id))
        except OrderError as exc:
            log.error(f"{ticker}: failed to place stop order: {exc}", exc_info=True)

        # Place limit sell orders at each target
        for level, price, shares in [
            ("t1", pos.t1_price, pos.t1_shares),
            ("t2", pos.t2_price, pos.t2_shares),
            ("t3", pos.t3_price, pos.t3_shares),
        ]:
            if shares <= 0:
                continue
            try:
                tgt_order = place_limit_sell(ticker, shares, price)
                tracker.mark_target_placed(ticker, level, str(tgt_order.id))
            except OrderError as exc:
                log.error(
                    f"{ticker}: failed to place {level.upper()} order: {exc}",
                    exc_info=True,
                )

    elif status in ("expired", "canceled", "done_for_day"):
        log.warning(
            f"{ticker}: entry order {status} (id={pos.entry_order_id})"
            " — removing from tracker"
        )
        tracker.mark_closed(ticker, status.upper())


def _check_exits(ticker: str, pos, tracker: PositionTracker) -> None:
    """Check stop and target order fills; update tracker accordingly."""
    pos = tracker.get(ticker)
    if pos is None:
        return

    # Stop has the highest priority — check it first
    if pos.stop_order_id and _is_filled(pos.stop_order_id, ticker, "stop"):
        log.info(f"{ticker}: stop order filled — cancelling target orders")
        for level in ("t1", "t2", "t3"):
            oid = getattr(pos, f"{level}_order_id")
            if oid and not getattr(pos, f"{level}_hit"):
                try:
                    cancel_order(oid)
                except OrderError as exc:
                    log.warning(f"{ticker}: could not cancel {level.upper()}: {exc}")
        tracker.mark_closed(ticker, "STOP")
        return

    # Reload after potential stop mutation
    pos = tracker.get(ticker)
    if pos is None or pos.status == "closed":
        return

    # Check each target in order
    for level in ("t1", "t2", "t3"):
        if getattr(pos, f"{level}_hit"):
            continue
        oid = getattr(pos, f"{level}_order_id")
        if oid and _is_filled(oid, ticker, level):
            tracker.mark_target_hit(ticker, level)
            pos = tracker.get(ticker)  # Reload after mutation

    # If nothing left open, close the position record
    pos = tracker.get(ticker)
    if pos and pos.shares_open <= 0:
        tracker.mark_closed(ticker, "ALL_TARGETS")


def _is_filled(order_id: str, ticker: str, label: str) -> bool:
    """Return True if the given order is in 'filled' status."""
    try:
        order = get_order(order_id)
        return str(order.status).lower() == "filled"
    except OrderError as exc:
        log.warning(f"{ticker}: could not check {label} order {order_id}: {exc}")
        return False


# ─── Background Monitor Thread ────────────────────────────────────────────── #


def _is_market_hours() -> bool:
    """
    Return True if NYSE is likely open (Mon–Fri, 09:30–16:15 ET).

    Uses America/New_York timezone.  On any error falls back to True so
    monitoring continues rather than silently stopping.
    """
    try:
        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz)
        if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return False
        return dtime(9, 30) <= now.time() <= dtime(16, 15)
    except Exception:
        return True  # Fail-open: keep monitoring on timezone errors


def _monitor_loop(tracker: PositionTracker, interval_sec: int) -> None:
    """Daemon loop: sleep then poll positions during market hours."""
    while True:
        try:
            time.sleep(interval_sec)
            if _is_market_hours():
                monitor_positions(tracker)
        except Exception as exc:
            log.error(f"Monitor loop unhandled error: {exc}", exc_info=True)


def start_monitor_thread(tracker: PositionTracker) -> threading.Thread:
    """
    Launch a background daemon thread that polls position state every
    MONITOR_INTERVAL_MIN minutes (during NYSE market hours only).

    The thread is a daemon so it exits automatically when the main process ends.

    Returns:
        The started Thread object.
    """
    interval_min = int(getattr(config, "MONITOR_INTERVAL_MIN", 5))
    interval_sec = interval_min * 60
    t = threading.Thread(
        target=_monitor_loop,
        args=(tracker, interval_sec),
        daemon=True,
        name="position-monitor",
    )
    t.start()
    log.info(
        f"Position monitor thread started"
        f" (interval={interval_min} min, market-hours only)"
    )
    return t
