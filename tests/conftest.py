"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

import src.utils.config as cfg


@pytest.fixture(autouse=True)
def _isolate_circuit_breaker_state(tmp_path, monkeypatch):
    """Point the P0.3 circuit-breaker state file at a per-test temp path.

    ``execute_trade_plan`` persists broker-error and halt state to
    ``config.CIRCUIT_STATE_PATH``. Without this, tests that drive the execution
    path would write to the real ``data/circuit_breaker.json`` and could trip the
    broker breaker for later tests (cross-test contamination). Each test gets a
    fresh, isolated state file. Individual tests may still override this path.
    """
    monkeypatch.setattr(cfg, "CIRCUIT_STATE_PATH", str(tmp_path / "circuit_breaker.json"))


@pytest.fixture(autouse=True)
def _isolate_execution_quality_ledger(tmp_path, monkeypatch):
    """Point the P2.1 execution-quality ledger at a per-test temp path.

    The executor records every fill to ``config.EXEC_QUALITY_LEDGER_PATH`` via a
    lazy singleton; isolate the path AND reset the singleton so execution tests
    never write to the real ``data/execution_quality.json`` or leak across tests.
    """
    import src.alpaca.execution_quality as _eq
    monkeypatch.setattr(cfg, "EXEC_QUALITY_LEDGER_PATH", str(tmp_path / "execution_quality.json"))
    monkeypatch.setattr(_eq, "_default", None)


@pytest.fixture(autouse=True)
def _isolate_metrics_ledger(tmp_path, monkeypatch):
    """Point the P2.3 scan-metrics ledger at a per-test temp path.

    ``run_daily_scan`` records each scan to ``config.METRICS_LEDGER_PATH`` via a
    lazy singleton; isolate the path AND reset the singleton so scan/metrics tests
    never write to the real ``data/metrics.json`` or leak across tests.
    """
    import src.observability.metrics as _metrics
    monkeypatch.setattr(cfg, "METRICS_LEDGER_PATH", str(tmp_path / "metrics.json"))
    monkeypatch.setattr(_metrics, "_default", None)


@pytest.fixture(autouse=True)
def _isolate_explain_ledger(tmp_path, monkeypatch):
    """Point the P3.1 explain daily-call ledger at a per-test temp path.

    The explainability layer writes a per-day call counter to
    ``config.EXPLAIN_CALL_LEDGER_PATH`` for its cost cap; isolate it so tests
    never touch the real ``data/explain_calls.json``.
    """
    monkeypatch.setattr(cfg, "EXPLAIN_CALL_LEDGER_PATH", str(tmp_path / "explain_calls.json"))
