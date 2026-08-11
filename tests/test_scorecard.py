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


def test_mark_closed_is_idempotent(tracker):
    # A repeated close must not double-book the trade ledger or realized P&L.
    tracker.add_position(_plan(), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_closed("AAA", "STOP", realized_pnl=-30.0)
    tracker.mark_closed("AAA", "STOP", realized_pnl=-30.0)  # duplicate call
    assert len(tracker.get_trades()) == 1
    from datetime import datetime
    assert tracker.realized_pnl_since(datetime(2000, 1, 1)) == pytest.approx(-30.0)


def test_trade_ledger_is_bounded(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_SCORECARD_MAX_TRADES", 3)
    for i in range(5):
        t = f"T{i}"
        tracker.add_position(_plan(ticker=t), f"oid{i}")
        tracker.mark_entry_filled(t, 100.0, 10)
        tracker.mark_closed(t, "STOP", realized_pnl=float(i))
    trades = tracker.get_trades()
    assert len(trades) == 3  # trimmed to the cap
    assert [x["ticker"] for x in trades] == ["T2", "T3", "T4"]  # newest kept


def test_load_drops_malformed_trade_records(tmp_path):
    import json
    store = tmp_path / "pos.json"
    store.write_text(json.dumps({
        "positions": {},
        "trades": [
            {"pnl": 10.0, "closed_at": "2026-01-01T00:00:00", "ticker": "OK"},
            {"pnl": None, "closed_at": "2026-01-01T00:00:00"},   # bad pnl
            {"pnl": 5.0},                                        # missing closed_at
            "not-a-dict",                                        # wrong type
        ],
    }), encoding="utf-8")
    t = PositionTracker(store_path=str(store))
    t.load()
    trades = t.get_trades()
    assert len(trades) == 1 and trades[0]["ticker"] == "OK"


def test_live_outcomes_excluded_from_paper_scorecard(tracker, monkeypatch):
    # A trade closed on the paper account and one on the live account.
    monkeypatch.setattr(cfg, "ALPACA_PAPER", True)
    tracker.add_position(_plan(ticker="AAA"), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_closed("AAA", "STOP", realized_pnl=-30.0)

    monkeypatch.setattr(cfg, "ALPACA_PAPER", False)  # graduated to real money
    tracker.add_position(_plan(ticker="BBB"), "oid2")
    tracker.mark_entry_filled("BBB", 100.0, 10)
    tracker.mark_closed("BBB", "STOP", realized_pnl=-500.0)

    assert len(tracker.get_trades()) == 2
    paper = tracker.get_trades(paper_only=True)
    assert len(paper) == 1 and paper[0]["ticker"] == "AAA"
    # The live -$500 loss must not enter the paper graduation scorecard.
    card = scorecard_from_tracker(tracker, initial_capital=10_000, n_bootstrap=100)
    assert card["n_trades"] == 1
    assert card["total_pnl"] == -30.0


def test_legacy_open_position_backfills_realized_pnl_on_load(tracker, tmp_path):
    # Simulate a pre-P0.4 in-flight position: a booked partial exit exists in the
    # realized ledger, but the position record's realized_pnl is stale at 0.
    tracker.add_position(_plan(), "oid1")
    tracker.mark_entry_filled("AAA", 100.0, 10)
    tracker.mark_target_hit("AAA", "t1", realized_pnl=45.0)  # books +45 to the ledger
    tracker._positions["AAA"].realized_pnl = 0.0  # corrupt as a legacy record would be
    tracker.save()

    reloaded = PositionTracker(store_path=str(tmp_path / "pos.json"))
    reloaded.load()
    assert reloaded._positions["AAA"].realized_pnl == pytest.approx(45.0)  # backfilled

    # And the final close now includes the pre-upgrade P&L in the trade outcome.
    reloaded.mark_closed("AAA", "STOP", realized_pnl=-15.0)
    assert reloaded.get_trades()[0]["pnl"] == pytest.approx(30.0)  # 45 - 15


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
