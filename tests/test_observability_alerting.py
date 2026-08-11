"""Tests for the P2.3 operational alerting (breaker trips, data gaps)."""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.observability import alerting


@pytest.fixture()
def captured(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "src.alerts.telegram_alert.send_text_alert",
        lambda text, context="alert": sent.append((context, text)) or True,
    )
    return sent


def test_alerts_disabled_by_default(monkeypatch, captured):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", False)
    assert alerting.alert_breaker_trip({"broker_error_count": 5}, kind="broker_breaker") is False
    assert alerting.alert_data_gap(7200, 3600) is False
    assert captured == []


def test_breaker_trip_alert_when_enabled(monkeypatch, captured):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", True)
    ok = alerting.alert_breaker_trip(
        {"broker_error_count": 5, "manual_halt_reason": ""}, kind="broker_breaker"
    )
    assert ok is True
    assert len(captured) == 1
    assert captured[0][0] == "breaker:broker_breaker"
    assert "broker_errors=5" in captured[0][1]


def test_manual_halt_alert_includes_reason(monkeypatch, captured):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", True)
    alerting.alert_breaker_trip({"manual_halt_reason": "hands off"}, kind="manual_halt")
    assert "hands off" in captured[0][1]


def test_data_gap_alert_when_enabled(monkeypatch, captured):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", True)
    assert alerting.alert_data_gap(7200, 3600) is True
    assert captured[0][0] == "data_gap"


def test_maybe_alert_data_gap_threshold(monkeypatch, captured):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "METRICS_DATA_GAP_SECONDS", 3600.0)
    # Under threshold → no alert.
    assert alerting.maybe_alert_data_gap(1800) is False
    # Over threshold → alert.
    assert alerting.maybe_alert_data_gap(7200) is True
    # None (no scans yet) → no alert.
    assert alerting.maybe_alert_data_gap(None) is False


def test_alerting_never_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(cfg, "OBSERVABILITY_ALERTS_ENABLED", True)

    def _boom(text, context="alert"):
        raise RuntimeError("network down")

    monkeypatch.setattr("src.alerts.telegram_alert.send_text_alert", _boom)
    # Swallowed → returns False, does not propagate.
    assert alerting.alert_data_gap(7200, 3600) is False


# ── executor breaker-trip transition ──────────────────────────────────────────

def test_executor_alerts_only_on_trip_transition(monkeypatch):
    import src.alpaca.executor as ex

    calls = []
    monkeypatch.setattr(
        "src.observability.alerting.alert_breaker_trip",
        lambda state, kind="breaker": calls.append(kind),
    )

    class _Breaker:
        def __init__(self):
            self._tripped = False
            self.errors = 0

        def broker_breaker_tripped(self):
            return self._tripped

        def record_broker_error(self, ctx):
            self.errors += 1
            if self.errors >= 2:
                self._tripped = True

        def state(self):
            return {"broker_error_count": self.errors}

    b = _Breaker()
    ex._record_broker_error(b, "err 1")   # not tripped yet
    assert calls == []
    ex._record_broker_error(b, "err 2")   # false→true transition → one alert
    assert calls == ["broker_breaker"]
    ex._record_broker_error(b, "err 3")   # already tripped → no repeat alert
    assert calls == ["broker_breaker"]
