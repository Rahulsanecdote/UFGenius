"""Tests for the P0.3 circuit breakers (global halt / broker-error / data-staleness).

Offline and deterministic — no broker, no network. Timing is driven by explicit
``now`` values and monkeypatched config thresholds so nothing depends on the
wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.utils.config as cfg
from src.alpaca.circuit_breaker import CircuitBreaker
from src.alpaca.executor import RiskGuard, execute_trade_plan
from src.alpaca.position_tracker import PositionTracker


def _now() -> datetime:
    return datetime(2026, 1, 2, 15, 30, 0)  # fixed naive-UTC instant


@pytest.fixture
def breaker(tmp_path):
    return CircuitBreaker(path=str(tmp_path / "cb.json"))


@pytest.fixture(autouse=True)
def _breaker_config(monkeypatch):
    # Deterministic breaker thresholds regardless of config.yaml.
    monkeypatch.setattr(cfg, "CIRCUIT_BROKER_ERROR_THRESHOLD", 3)
    monkeypatch.setattr(cfg, "CIRCUIT_BROKER_ERROR_WINDOW_SECONDS", 300.0)
    monkeypatch.setattr(cfg, "CIRCUIT_DATA_STALENESS_MAX_SECONDS", 120.0)


# ── manual halt + persistence ─────────────────────────────────────────────────

def test_halt_and_resume_persist_across_instances(tmp_path):
    path = str(tmp_path / "cb.json")
    CircuitBreaker(path=path).halt("maintenance")
    reloaded = CircuitBreaker(path=path).load()
    assert reloaded.manual_halt is True
    blocked, reason = reloaded.blocks_entry({}, now=_now())
    assert blocked and "halted" in reason.lower() and "maintenance" in reason

    CircuitBreaker(path=path).resume()
    assert CircuitBreaker(path=path).load().manual_halt is False


def test_load_missing_or_malformed_file_is_healthy(tmp_path):
    missing = CircuitBreaker(path=str(tmp_path / "nope.json")).load()
    assert missing.blocks_entry({}, now=_now())[0] is False

    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    assert CircuitBreaker(path=str(bad)).load().blocks_entry({}, now=_now())[0] is False

    notobj = tmp_path / "list.json"
    notobj.write_text("[1, 2, 3]", encoding="utf-8")
    assert CircuitBreaker(path=str(notobj)).load().manual_halt is False


# ── broker-error breaker ──────────────────────────────────────────────────────

def test_broker_breaker_trips_at_threshold(breaker):
    t = _now()
    for i in range(2):
        breaker.record_broker_error("boom", now=t + timedelta(seconds=i))
    assert breaker.broker_breaker_tripped(now=t + timedelta(seconds=2)) is False
    breaker.record_broker_error("boom", now=t + timedelta(seconds=2))
    assert breaker.broker_breaker_tripped(now=t + timedelta(seconds=2)) is True
    blocked, reason = breaker.blocks_entry({}, now=t + timedelta(seconds=2))
    assert blocked and "broker" in reason.lower()


def test_broker_errors_age_out_of_window(breaker):
    t = _now()
    for i in range(3):
        breaker.record_broker_error("boom", now=t + timedelta(seconds=i))
    assert breaker.broker_breaker_tripped(now=t + timedelta(seconds=2)) is True
    # The last error is at t+2, so all three only leave the 300s window past t+302.
    later = t + timedelta(seconds=303)
    assert breaker.broker_error_count(now=later) == 0
    assert breaker.broker_breaker_tripped(now=later) is False


def test_resume_clears_broker_error_trail(breaker):
    t = _now()
    for i in range(3):
        breaker.record_broker_error("boom", now=t + timedelta(seconds=i))
    assert breaker.broker_breaker_tripped(now=t) is True
    breaker.resume()
    assert breaker.broker_error_count(now=t) == 0


def test_broker_breaker_disabled_when_threshold_non_positive(breaker, monkeypatch):
    monkeypatch.setattr(cfg, "CIRCUIT_BROKER_ERROR_THRESHOLD", 0)
    t = _now()
    for i in range(10):
        breaker.record_broker_error("boom", now=t + timedelta(seconds=i))
    assert breaker.broker_breaker_tripped(now=t + timedelta(seconds=10)) is False


# ── data-staleness breaker ────────────────────────────────────────────────────

def test_stale_data_blocks_entry(breaker):
    t = _now()
    plan = {"quote_as_of": (t - timedelta(seconds=200)).isoformat()}
    blocked, reason = breaker.blocks_entry(plan, now=t)
    assert blocked and "stale" in reason.lower()


def test_fresh_data_allows_entry(breaker):
    t = _now()
    plan = {"quote_as_of": (t - timedelta(seconds=10)).isoformat()}
    assert breaker.blocks_entry(plan, now=t)[0] is False


def test_missing_timestamp_fails_open(breaker):
    # Unknown data age cannot fire the staleness gate.
    assert breaker.blocks_entry({}, now=_now())[0] is False
    assert breaker.blocks_entry({"quote_as_of": "not-a-date"}, now=_now())[0] is False


def test_staleness_disabled_when_limit_non_positive(breaker, monkeypatch):
    monkeypatch.setattr(cfg, "CIRCUIT_DATA_STALENESS_MAX_SECONDS", 0.0)
    t = _now()
    plan = {"quote_as_of": (t - timedelta(hours=5)).isoformat()}
    assert breaker.blocks_entry(plan, now=t)[0] is False


def test_tz_aware_timestamp_is_handled(breaker):
    t = _now()
    aware = (t.replace(tzinfo=timezone.utc) - timedelta(seconds=200)).isoformat()
    assert breaker.blocks_entry({"quote_as_of": aware}, now=t)[0] is True


# ── priority ordering ─────────────────────────────────────────────────────────

def test_manual_halt_takes_priority_over_other_breakers(breaker):
    t = _now()
    breaker.halt("operator stop")
    for i in range(5):
        breaker.record_broker_error("boom", now=t + timedelta(seconds=i))
    plan = {"quote_as_of": (t - timedelta(seconds=999)).isoformat()}
    blocked, reason = breaker.blocks_entry(plan, now=t)
    assert blocked and "halted" in reason.lower()  # halt reason wins


# ── RiskGuard integration ─────────────────────────────────────────────────────

def _plan(ticker="AAPL", signal="STRONG_BUY"):
    return {
        "ticker": ticker, "signal": signal,
        "entry": {"type": "LIMIT", "price": 189.40},
        "stop_loss": {"price": 186.35},
        "targets": {"T1": {"price": 191.69}, "T2": {"price": 196.07}, "T3": {"price": 203.84}},
        "position": {"shares": 10, "position_value": 1894.0, "risk_dollars": 30.5},
        "reasoning": ["Golden Cross"],
    }


def _portfolio():
    return {"total_equity": 50_000.0, "buying_power": 45_000.0, "position_count": 0}


@pytest.fixture
def tracker(tmp_path):
    return PositionTracker(store_path=str(tmp_path / "pos.json"))


def test_riskguard_blocks_when_halted(tracker, breaker, monkeypatch):
    # Keep the earnings lookup offline (config default trade_earnings_week: false).
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    breaker.halt("test halt")
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker, breaker)
    assert ok is False and "halted" in reason.lower()


def test_riskguard_without_breaker_is_unchanged(tracker, monkeypatch):
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)  # 3-arg form
    assert ok is True and reason == ""


# ── end-to-end execution path ─────────────────────────────────────────────────

def test_execute_trade_plan_halted_blocks_order(tracker, tmp_path, monkeypatch):
    import src.alpaca.executor as _ex
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    state_path = str(tmp_path / "cb.json")
    monkeypatch.setattr(cfg, "CIRCUIT_STATE_PATH", state_path)
    CircuitBreaker(path=state_path).halt("halted from dashboard")

    entry = MagicMock()
    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order", return_value=entry) as submit:
            result = execute_trade_plan(_plan(), tracker)
    assert result["ok"] is False and "halted" in result["reason"].lower()
    submit.assert_not_called()  # no order ever reached the broker


def test_execute_trade_plan_records_broker_error_on_submit_failure(tracker, tmp_path, monkeypatch):
    import src.alpaca.executor as _ex
    from src.alpaca.orders import OrderError
    monkeypatch.setattr(_ex, "_lookup_days_to_earnings", lambda _t: None)
    state_path = str(tmp_path / "cb.json")
    monkeypatch.setattr(cfg, "CIRCUIT_STATE_PATH", state_path)

    with patch("src.alpaca.executor.get_portfolio_data", return_value=_portfolio()):
        with patch("src.alpaca.executor.place_entry_order", side_effect=OrderError("boom")):
            result = execute_trade_plan(_plan(), tracker)
    assert result["ok"] is False
    # The failure was recorded on the shared breaker state.
    assert CircuitBreaker(path=state_path).load().broker_error_count(now=None) >= 1
