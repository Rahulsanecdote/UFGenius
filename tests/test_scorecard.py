"""Tests for the P0.4 paper-trading scorecard and its live-performance gate."""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.alpaca.executor import RiskGuard
from src.alpaca.position_tracker import PositionTracker
from src.alpaca.scorecard import (
    compute_scorecard,
    meets_live_performance_gate,
    scorecard_from_tracker,
)


def _trades(pnls: list[float]) -> list[dict]:
    return [{"pnl": float(p), "closed_at": "2026-01-01T00:00:00", "ticker": "AAA"} for p in pnls]


@pytest.fixture(autouse=True)
def _scorecard_config(monkeypatch):
    # Deterministic, test-friendly floors.
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_MIN_TRADES", 10)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PROFIT_FACTOR_FLOOR", 1.2)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PROB_PROFITABLE_FLOOR", 0.55)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_REQUIRE_POSITIVE_EXPECTANCY", True)
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED", True)


# ── compute_scorecard ─────────────────────────────────────────────────────────

def test_empty_ledger_is_not_acceptable():
    card = compute_scorecard([], initial_capital=10_000)
    assert card["n_trades"] == 0
    assert card["acceptance"]["all_pass"] is False


def test_winning_record_passes_all_floors():
    # 14 wins of +100, 6 losses of -50 → PF 4.67, expectancy +55, prob-prof ~1.
    card = compute_scorecard(_trades([100] * 14 + [-50] * 6), initial_capital=10_000, n_bootstrap=300)
    assert card["n_trades"] == 20
    assert card["win_rate_pct"] == 70.0
    assert card["profit_factor"] == pytest.approx(1400 / 300, rel=1e-3)
    assert card["expectancy_per_trade"] == pytest.approx(55.0)
    assert card["acceptance"]["all_pass"] is True


def test_losing_record_fails_on_expectancy():
    card = compute_scorecard(_trades([50] * 6 + [-100] * 14), initial_capital=10_000, n_bootstrap=300)
    assert card["expectancy_per_trade"] < 0
    assert card["acceptance"]["checks"]["positive_expectancy_ok"] is False
    assert card["acceptance"]["all_pass"] is False


def test_too_few_trades_blocks_even_if_profitable():
    card = compute_scorecard(_trades([100, 100, 100]), initial_capital=10_000, n_bootstrap=200)
    assert card["acceptance"]["checks"]["sufficient_trades_ok"] is False
    assert card["acceptance"]["all_pass"] is False


def test_lossless_record_passes_profit_factor_check():
    # No losses → profit_factor is None but must not block on the PF gate.
    card = compute_scorecard(_trades([100] * 12), initial_capital=10_000, n_bootstrap=200)
    assert card["profit_factor"] is None
    assert card["acceptance"]["checks"]["profit_factor_ok"] is True
    assert card["acceptance"]["all_pass"] is True


def test_metrics_match_shared_bootstrap_estimator():
    # prob_profitable comes from the same estimator the backtest validation uses.
    from src.backtest.validation import bootstrap_trade_metrics

    pnls = [100] * 14 + [-50] * 6
    card = compute_scorecard(_trades(pnls), initial_capital=10_000, n_bootstrap=300, seed=7)
    direct = bootstrap_trade_metrics(pnls, 10_000, n_resamples=300, seed=7)
    assert card["prob_profitable"] == direct["prob_profitable"]


# ── tracker trade-outcome ledger ──────────────────────────────────────────────

def _plan(ticker="AAA", signal="STRONG_BUY", score=82.0):
    return {
        "ticker": ticker, "signal": signal, "composite_score": score,
        "entry": {"price": 100.0}, "stop_loss": {"price": 95.0},
        "targets": {"T1": {"price": 110}, "T2": {"price": 120}, "T3": {"price": 130}},
        "position": {"shares": 10, "risk_dollars": 50.0},
    }


@pytest.fixture
def tracker(tmp_path):
    return PositionTracker(store_path=str(tmp_path / "pos.json"))


def test_closed_filled_position_records_a_trade(tracker):
    tracker.add_position(_plan(), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_closed("AAA", "STOP", realized_pnl=-30.0)
    trades = tracker.get_trades()
    assert len(trades) == 1
    assert trades[0]["pnl"] == -30.0
    assert trades[0]["return_pct"] == -3.0  # -30 / (100*10) * 100
    assert trades[0]["signal"] == "STRONG_BUY"
    assert trades[0]["composite_score"] == 82.0


def test_unfilled_position_records_no_trade(tracker):
    tracker.add_position(_plan(), "oid1")  # never filled (fill_price stays None)
    tracker.mark_closed("AAA", "EXPIRED")
    assert tracker.get_trades() == []


def test_trade_ledger_persists_across_reload(tracker, tmp_path):
    tracker.add_position(_plan(), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_closed("AAA", "ALL_TARGETS", realized_pnl=120.0)
    reloaded = PositionTracker(store_path=str(tmp_path / "pos.json"))
    reloaded.load()
    assert len(reloaded.get_trades()) == 1
    assert reloaded.get_trades()[0]["pnl"] == 120.0


def test_multiple_tranche_exits_aggregate_into_one_trade(tracker):
    tracker.add_position(_plan(), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_target_hit("AAA", "t1", realized_pnl=30.0)
    tracker.mark_target_hit("AAA", "t2", realized_pnl=80.0)
    tracker.mark_closed("AAA", "STOP", realized_pnl=-10.0)
    trades = tracker.get_trades()
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(100.0)  # 30 + 80 - 10


# ── live-performance gate ─────────────────────────────────────────────────────

def test_gate_disabled_returns_pass(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED", False)
    passes, card = meets_live_performance_gate(tracker, initial_capital=10_000)
    assert passes is True and card is None


def test_gate_blocks_on_failing_scorecard(tracker):
    tracker._trades = _trades([50] * 6 + [-100] * 14)  # net negative
    passes, card = meets_live_performance_gate(tracker, initial_capital=10_000)
    assert passes is False
    assert card["acceptance"]["all_pass"] is False


def test_gate_passes_on_strong_scorecard(tracker):
    tracker._trades = _trades([100] * 14 + [-50] * 6)
    passes, _ = meets_live_performance_gate(tracker, initial_capital=10_000)
    assert passes is True


# ── RiskGuard integration on the live path ────────────────────────────────────

def _portfolio():
    return {"total_equity": 50_000.0, "buying_power": 45_000.0, "position_count": 0}


def _entry_plan():
    return {
        "ticker": "MSFT", "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": 189.40}, "stop_loss": {"price": 186.35},
        "targets": {"T1": {"price": 191.69}, "T2": {"price": 196.07}, "T3": {"price": 203.84}},
        "position": {"shares": 10, "position_value": 1894.0, "risk_dollars": 30.5},
        "reasoning": ["Golden Cross"],
    }


def test_riskguard_live_blocks_when_scorecard_fails(tracker, monkeypatch):
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    monkeypatch.setattr(cfg, "SAFETY", {})  # isolate the performance gate
    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)
    tracker._trades = _trades([50] * 6 + [-100] * 14)  # failing paper record
    ok, reason = RiskGuard().check(_entry_plan(), _portfolio(), tracker)
    assert ok is False and "scorecard" in reason.lower()


def test_riskguard_live_allows_when_scorecard_passes(tracker, monkeypatch):
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    monkeypatch.setattr(cfg, "SAFETY", {})
    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)
    tracker._trades = _trades([100] * 14 + [-50] * 6)  # strong paper record
    ok, reason = RiskGuard().check(_entry_plan(), _portfolio(), tracker)
    assert ok is True, reason
