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
