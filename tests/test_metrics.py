"""Tests for the P2.3 scan-metrics ledger."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import src.utils.config as cfg
from src.observability.metrics import MetricsLedger, _percentile


@pytest.fixture()
def ledger(tmp_path):
    return MetricsLedger(path=str(tmp_path / "metrics.json"))


def test_record_and_summary_basic(ledger):
    ledger.record_scan(12.5, total_scanned=100, total_signals=8,
                        label_counts={"STRONG_BUY": 2, "BUY": 3, "WEAK_BUY": 3}, regime="BULL")
    s = ledger.summary()
    assert s["n_scans"] == 1
    assert s["last_scan_latency_sec"] == 12.5
    assert s["avg_scan_latency_sec"] == 12.5
    assert s["last_total_signals"] == 8
    assert s["last_label_counts"] == {"STRONG_BUY": 2, "BUY": 3, "WEAK_BUY": 3}
    assert s["last_regime"] == "BULL"


def test_summary_empty(ledger):
    s = ledger.summary()
    assert s["n_scans"] == 0
    assert "note" in s


def test_percentile_nearest_rank():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(xs, 0.0) == 1.0
    assert _percentile(xs, 100.0) == 5.0
    assert _percentile(xs, 50.0) == 3.0
    assert _percentile([], 95.0) is None
    assert _percentile([7.0], 95.0) == 7.0
    # Nearest-rank (not interpolation): P95 of 1..10 is an observed value, 10.0.
    assert _percentile([float(i) for i in range(1, 11)], 95.0) == 10.0


def test_latency_stats_over_many(ledger):
    for i in range(10):
        ledger.record_scan(float(i + 1), total_scanned=10, total_signals=i)
    s = ledger.summary()
    assert s["n_scans"] == 10
    assert s["avg_scan_latency_sec"] == 5.5
    # Nearest-rank P95 of latencies 1..10 = 10.0, not the interpolated 9.55.
    assert s["p95_scan_latency_sec"] == 10.0
    assert s["last_scan_latency_sec"] == 10.0


def test_two_writers_reload_no_lost_record(tmp_path):
    # Two ledger instances on the same file model two processes. Each write
    # reloads under the interprocess lock before appending, so neither record is
    # clobbered (last-writer-wins would leave only one).
    path = str(tmp_path / "metrics.json")
    a = MetricsLedger(path=path)
    b = MetricsLedger(path=path)
    a.record_scan(1.0, total_scanned=1, total_signals=0)
    b.record_scan(2.0, total_scanned=1, total_signals=0)
    assert MetricsLedger(path=path).load().summary()["n_scans"] == 2


def test_data_gap_flag(ledger, monkeypatch):
    monkeypatch.setattr(cfg, "METRICS_DATA_GAP_SECONDS", 3600.0)
    base = datetime(2026, 1, 1, 12, 0, 0)
    ledger.record_scan(5.0, total_scanned=10, total_signals=1, now=base)
    # Just after: no gap.
    s = ledger.summary(now=base + timedelta(minutes=10))
    assert s["data_gap"] is False
    assert s["seconds_since_last_scan"] == 600.0
    # Two hours later: gap.
    s2 = ledger.summary(now=base + timedelta(hours=2))
    assert s2["data_gap"] is True


def test_data_gap_disabled_when_threshold_zero(ledger, monkeypatch):
    monkeypatch.setattr(cfg, "METRICS_DATA_GAP_SECONDS", 0.0)
    base = datetime(2026, 1, 1, 12, 0, 0)
    ledger.record_scan(5.0, total_scanned=10, total_signals=1, now=base)
    s = ledger.summary(now=base + timedelta(days=5))
    assert s["data_gap"] is False


def test_bounded_by_cap(ledger, monkeypatch):
    monkeypatch.setattr(cfg, "METRICS_MAX_SCANS", 3)
    for i in range(6):
        ledger.record_scan(float(i), total_scanned=1, total_signals=0)
    assert len(ledger.scans()) == 3
    # Kept the most recent.
    assert ledger.scans()[-1]["elapsed_sec"] == 5.0


def test_record_rejects_non_numeric(ledger):
    assert ledger.record_scan("abc", total_scanned=1, total_signals=0) is None


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "metrics.json")
    MetricsLedger(path=path).record_scan(3.0, total_scanned=5, total_signals=2, regime="BULL")
    reloaded = MetricsLedger(path=path).load()
    assert reloaded.summary()["n_scans"] == 1
    assert reloaded.summary()["last_regime"] == "BULL"


def test_module_record_scan_best_effort(monkeypatch, tmp_path):
    # The module-level wrapper never raises even if the ledger blows up.
    import src.observability.metrics as m
    monkeypatch.setattr(cfg, "METRICS_LEDGER_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setattr(m, "_default", None)

    class _Boom(m.MetricsLedger):
        def record_scan(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(m, "MetricsLedger", _Boom)
    monkeypatch.setattr(m, "_default", None)
    assert m.record_scan(1.0, 1, 0) is None  # swallowed, not raised
