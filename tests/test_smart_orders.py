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
