"""Tests for the P2.2 smart order handling (marketable-limit pricing)."""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.alpaca.smart_orders import (
    entry_offset_pct,
    marketable_limit_price,
    smart_entry_price,
)


@pytest.fixture(autouse=True)
def _smart_config(monkeypatch):
    monkeypatch.setattr(cfg, "SMART_ORDERS_ENTRY_OFFSET_FLOOR_PCT", 0.001)
    monkeypatch.setattr(cfg, "SMART_ORDERS_ENTRY_OFFSET_CAP_PCT", 0.01)
    monkeypatch.setattr(cfg, "SMART_ORDERS_ENABLED", True)


# ── marketable_limit_price ────────────────────────────────────────────────────

def test_marketable_buy_crosses_up():
    assert marketable_limit_price("buy", 100.0, 0.005) == 100.5


def test_marketable_sell_crosses_down():
    assert marketable_limit_price("sell", 50.0, 0.004) == 49.8


def test_non_positive_reference_returns_reference():
    assert marketable_limit_price("buy", 0.0, 0.01) == 0.0


def test_negative_offset_treated_as_zero():
    assert marketable_limit_price("buy", 100.0, -0.01) == 100.0


# ── entry_offset_pct (measured-slippage-tuned, clamped) ───────────────────────

def test_offset_uses_measured_within_bounds():
    assert entry_offset_pct(0.005) == 0.005


def test_offset_floored():
    assert entry_offset_pct(0.0) == 0.001       # below floor → floor
    assert entry_offset_pct(None) == 0.001      # unknown → floor


def test_offset_capped():
    assert entry_offset_pct(0.05) == 0.01       # above cap → cap


# ── smart_entry_price ─────────────────────────────────────────────────────────

def test_smart_entry_disabled_is_plain(monkeypatch):
    monkeypatch.setattr(cfg, "SMART_ORDERS_ENABLED", False)
    assert smart_entry_price(100.0, measured=0.005) == 100.0  # unchanged


def test_smart_entry_enabled_is_marketable():
    assert smart_entry_price(100.0, measured=0.005) == 100.5
    assert smart_entry_price(100.0, measured=None) == 100.1   # floor
    assert smart_entry_price(100.0, measured=0.05) == 101.0   # cap


def test_smart_entry_reads_measured_slippage(monkeypatch):
    # With no explicit measured value it pulls from the P2.1 ledger.
    import src.alpaca.execution_quality as eq
    monkeypatch.setattr(eq, "measured_slippage_pct", lambda: 0.004)
    assert smart_entry_price(100.0) == 100.4


def test_smart_entry_crosses_market_not_accounting():
    # Crossing the *market* (100.0), not the discounted plan price (99.8). A
    # limit derived from the discount (99.8 * 1.005 ≈ 100.30) would be less
    # marketable than crossing the live market.
    assert smart_entry_price(100.0, accounting_price=99.8, measured=0.005) == 100.5


def test_smart_entry_cap_is_relative_to_accounting():
    # Cap bounds the chase relative to the PLAN price, not the market: with a
    # large measured slippage the market-cross (100 * 1.05 = 105) is clipped to
    # accounting * (1 + cap) = 98 * 1.01 = 98.98.
    assert smart_entry_price(100.0, accounting_price=98.0, measured=0.05) == 98.98


def test_smart_entry_non_positive_market_falls_back_to_accounting():
    assert smart_entry_price(0.0, accounting_price=99.8, measured=0.005) == 99.8


# ── executor integration ──────────────────────────────────────────────────────

def _plan():
    return {
        "ticker": "AAA", "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": 100.0}, "stop_loss": {"price": 96.0},
        "targets": {"T1": {"price": 103}, "T2": {"price": 106}, "T3": {"price": 110}},
        "position": {"shares": 10, "position_value": 1000.0, "risk_dollars": 40.0},
        "reasoning": ["test"],
    }


def _portfolio():
    return {"total_equity": 50_000.0, "buying_power": 45_000.0, "position_count": 0}


def test_execute_submits_marketable_limit_when_enabled(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    import src.alpaca.executor as ex
    from src.alpaca.position_tracker import PositionTracker

    monkeypatch.setattr(cfg, "SAFETY", {})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(ex, "_lookup_days_to_earnings", lambda _t: None)
    # measured slippage 0.5% → marketable buy at 100.5.
    import src.alpaca.execution_quality as eq
    monkeypatch.setattr(eq, "measured_slippage_pct", lambda: 0.005)

    tracker = PositionTracker(store_path=str(tmp_path / "pos.json"))
    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order", return_value=MagicMock(id="oid")) as submit:
            result = execute = ex.execute_trade_plan(_plan(), tracker)
    assert result["ok"] is True
    # place_entry_order called with the marketable limit, not the plain 100.0.
    assert submit.call_args.args[2] == 100.5
    assert result["limit_price"] == 100.5


def test_execute_submits_plain_limit_when_disabled(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    import src.alpaca.executor as ex
    from src.alpaca.position_tracker import PositionTracker

    monkeypatch.setattr(cfg, "SMART_ORDERS_ENABLED", False)
    monkeypatch.setattr(cfg, "SAFETY", {})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(ex, "_lookup_days_to_earnings", lambda _t: None)

    tracker = PositionTracker(store_path=str(tmp_path / "pos.json"))
    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order", return_value=MagicMock(id="oid")) as submit:
            result = ex.execute_trade_plan(_plan(), tracker)
    assert submit.call_args.args[2] == 100.0  # plain plan price


def test_riskguard_sees_position_sized_at_submit_price(tmp_path, monkeypatch):
    # The money-path safety fix: RiskGuard must gate on the marketable submit
    # price, not the discounted plan price, so a chase up to the cap cannot slip
    # past the exposure/per-trade-risk limits.
    from unittest.mock import MagicMock, patch

    import src.alpaca.executor as ex
    from src.alpaca.position_tracker import PositionTracker

    monkeypatch.setattr(cfg, "SAFETY", {})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    monkeypatch.setattr(ex, "_lookup_days_to_earnings", lambda _t: None)
    import src.alpaca.execution_quality as eq
    monkeypatch.setattr(eq, "measured_slippage_pct", lambda: 0.005)  # → submit 100.5

    seen = {}

    def _capture(self, plan, portfolio, tracker, breaker):
        seen["plan"] = plan
        return True, ""

    monkeypatch.setattr(ex.RiskGuard, "check", _capture)

    plan = _plan()  # entry price 100.0, stop 96.0, 10 shares
    tracker = PositionTracker(store_path=str(tmp_path / "pos.json"))
    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order", return_value=MagicMock(id="oid")):
            ex.execute_trade_plan(plan, tracker)

    pos = seen["plan"]["position"]
    assert pos["position_value"] == 1005.0                 # 100.5 × 10, not 1000
    assert pos["risk_dollars"] == round((100.5 - 96.0) * 10, 2)  # 45.0, not 40
    # Original plan dict is not mutated.
    assert plan["position"]["position_value"] == 1000.0
