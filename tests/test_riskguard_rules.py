"""Tests for the newly-enforced RiskGuard safety rules and realized-P&L ledger.

Each rule is isolated by monkeypatching config.SAFETY to just that rule, so the
earlier RiskGuard checks pass on their defaults and we assert the rule fires.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import src.utils.config as cfg
from src.alpaca.executor import RiskGuard, _check_exits
from src.alpaca.position_tracker import PositionTracker


def _plan(ticker="AAPL", signal="STRONG_BUY", stop=186.35):
    return {
        "ticker": ticker, "signal": signal,
        "entry": {"type": "LIMIT", "price": 189.40},
        "stop_loss": {"price": stop} if stop is not None else {},
        "targets": {
            "T1": {"price": 191.69}, "T2": {"price": 196.07}, "T3": {"price": 203.84},
        },
        "position": {"shares": 10, "position_value": 1894.0, "risk_dollars": 30.5},
        "reasoning": ["Golden Cross"],
    }


def _portfolio(equity=50_000.0, buying_power=45_000.0, position_count=0):
    return {"total_equity": equity, "buying_power": buying_power,
            "position_count": position_count}


@pytest.fixture
def tracker(tmp_path):
    return PositionTracker(store_path=str(tmp_path / "pos.json"))


# ── stop_loss_required ──────────────────────────────────────────────────────
def test_stop_loss_required_rejects_plan_without_stop(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"stop_loss_required": True})
    plan = _plan(stop=None)
    ok, reason = RiskGuard().check(plan, _portfolio(), tracker)
    assert not ok and "stop_loss_required" in reason


def test_stop_loss_required_allows_plan_with_stop(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"stop_loss_required": True})
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


# ── max_daily_loss_pct ──────────────────────────────────────────────────────
def test_daily_loss_limit_blocks_further_trades(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"max_daily_loss_pct": 2.0})
    tracker.record_realized("X", -1200)  # > 2% of $50k ($1,000)
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert not ok and "Daily loss limit" in reason


def test_daily_loss_within_limit_allows(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"max_daily_loss_pct": 2.0})
    tracker.record_realized("X", -500)  # under the $1,000 limit
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


# ── max_weekly_loss_pct ─────────────────────────────────────────────────────
def test_weekly_loss_limit_blocks(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"max_weekly_loss_pct": 5.0})
    tracker.record_realized("X", -3000)  # > 5% of $50k ($2,500)
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert not ok and "Weekly loss limit" in reason


# ── cooldown_after_loss_hours ───────────────────────────────────────────────
def test_cooldown_blocks_after_recent_loss(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"cooldown_after_loss_hours": 24})
    tracker.record_realized("X", -100, now=datetime.utcnow() - timedelta(hours=2))
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert not ok and "cooldown" in reason.lower()


def test_cooldown_elapsed_allows(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"cooldown_after_loss_hours": 24})
    tracker.record_realized("X", -100, now=datetime.utcnow() - timedelta(hours=48))
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


# ── trade_earnings_week ─────────────────────────────────────────────────────
def test_earnings_week_blocks_when_earnings_imminent(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    plan = _plan()
    plan["days_to_earnings"] = 3
    ok, reason = RiskGuard().check(plan, _portfolio(), tracker)
    assert not ok and "Earnings" in reason


def test_earnings_week_allows_when_unknown_or_far(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    for days in (None, 20):
        plan = _plan()
        plan["days_to_earnings"] = days
        ok, reason = RiskGuard().check(plan, _portfolio(), tracker)
        assert ok, f"days={days}: {reason}"


# ── paper_trade_days_required ───────────────────────────────────────────────
def test_paper_days_required_blocks_live_without_history(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"paper_trade_days_required": 30})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)  # live account
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert not ok and "paper_trade_days_required" in reason


def test_paper_days_required_allows_with_tenure(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"paper_trade_days_required": 30})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)
    tracker._trading_since = (datetime.utcnow() - timedelta(days=40)).isoformat()
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


def test_paper_days_required_ignored_on_paper_account(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"paper_trade_days_required": 30})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)  # paper — gate does not apply
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


# ── realized-P&L ledger + persistence ───────────────────────────────────────
def test_realized_ledger_queries_and_persist(tmp_path):
    p = str(tmp_path / "pos.json")
    t = PositionTracker(store_path=p)
    t.load()
    t.record_realized("A", -200)
    t.record_realized("B", 150)
    since = datetime.utcnow() - timedelta(hours=1)
    assert t.realized_pnl_since(since) == pytest.approx(-50.0)
    assert t.last_loss_time() is not None

    reloaded = PositionTracker(store_path=p)
    reloaded.load()
    assert reloaded.realized_pnl_since(since) == pytest.approx(-50.0)


# ── executor books realized P&L at exits ────────────────────────────────────
def _add_active(t):
    t.add_position(
        {
            "ticker": "AAPL", "entry": {"price": 189.40},
            "stop_loss": {"price": 186.35},
            "targets": {"T1": {"price": 191.69}, "T2": {"price": 196.07},
                        "T3": {"price": 203.84}},
            "position": {"shares": 10, "risk_dollars": 30.5},
        },
        "entry-id",
    )
    t.mark_entry_filled("AAPL", 189.50, 10)
    t.mark_stop_placed("AAPL", "stop-order-id", 10)
    t.mark_target_placed("AAPL", "t1", "t1-order-id")
    t.mark_target_placed("AAPL", "t2", "t2-order-id")
    t.mark_target_placed("AAPL", "t3", "t3-order-id")


def test_stop_exit_books_realized_loss(tracker):
    _add_active(tracker)

    def gos(oid):
        return MagicMock(status="filled" if oid == "stop-order-id" else "open")

    with patch("src.alpaca.executor.get_order", side_effect=gos):
        with patch("src.alpaca.executor.cancel_order", return_value=True):
            _check_exits("AAPL", tracker.get("AAPL"), tracker)

    # (186.35 - 189.50) * 10 shares = -31.50
    since = datetime.utcnow() - timedelta(hours=1)
    assert tracker.realized_pnl_since(since) == pytest.approx(-31.5)
    assert tracker.last_loss_time() is not None


def test_target_exit_books_realized_gain(tracker):
    _add_active(tracker)

    def gos(oid):
        return MagicMock(status="filled" if oid == "t1-order-id" else "open")

    with patch("src.alpaca.executor.get_order", side_effect=gos):
        with patch("src.alpaca.executor.cancel_order", return_value=True):
            with patch("src.alpaca.executor.place_stop_order", return_value=MagicMock(id="s2")):
                _check_exits("AAPL", tracker.get("AAPL"), tracker)

    # (191.69 - 189.50) * 3 shares (t1 tranche) = +6.57
    since = datetime.utcnow() - timedelta(hours=1)
    assert tracker.realized_pnl_since(since) == pytest.approx(6.57)
