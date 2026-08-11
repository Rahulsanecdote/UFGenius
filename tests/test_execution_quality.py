"""Tests for the P2.1 execution-quality measurement."""

from __future__ import annotations

import json

import pytest

import src.utils.config as cfg
from src.alpaca.execution_quality import ExecutionQualityLedger, _adverse_slippage_bps


def _ledger(tmp_path):
    return ExecutionQualityLedger(path=str(tmp_path / "eq.json"))


# ── slippage math ─────────────────────────────────────────────────────────────

def test_adverse_slippage_sign_convention():
    # Buy: fill above expected is adverse (positive).
    assert _adverse_slippage_bps("buy", 100.0, 100.10) == pytest.approx(10.0)
    # Buy: fill below expected is favorable (negative).
    assert _adverse_slippage_bps("buy", 100.0, 99.90) == pytest.approx(-10.0)
    # Sell: fill below expected is adverse (positive).
    assert _adverse_slippage_bps("sell", 50.0, 49.95) == pytest.approx(10.0)
    # Sell: fill above expected is favorable (negative).
    assert _adverse_slippage_bps("sell", 50.0, 50.05) == pytest.approx(-10.0)


def test_adverse_slippage_guards():
    assert _adverse_slippage_bps("buy", 0.0, 1.0) is None      # zero expected
    assert _adverse_slippage_bps("buy", "x", 1.0) is None      # non-numeric


# ── recording + persistence ───────────────────────────────────────────────────

def test_record_and_reload(tmp_path):
    led = _ledger(tmp_path)
    rec = led.record_fill("aaa", "buy", "entry", 100.0, 100.20, 10, order_id="o1")
    assert rec["slippage_bps"] == pytest.approx(20.0)
    assert rec["implementation_shortfall"] == pytest.approx(2.0)  # 0.20 * 10
    assert rec["ticker"] == "AAA" and rec["order_id"] == "o1"
    reloaded = ExecutionQualityLedger(path=str(tmp_path / "eq.json")).load()
    assert len(reloaded.fills()) == 1


def test_ledger_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "EXEC_QUALITY_MAX_FILLS", 3)
    led = _ledger(tmp_path)
    for i in range(5):
        led.record_fill(f"T{i}", "buy", "entry", 100.0, 100.1, 1)
    assert len(led.fills()) == 3  # trimmed to cap


def test_uncomputable_fill_is_not_recorded(tmp_path):
    led = _ledger(tmp_path)
    assert led.record_fill("AAA", "buy", "entry", 0.0, 1.0, 10) is None
    assert led.fills() == []


# ── summary + measured slippage ───────────────────────────────────────────────

def test_summary_and_measured_slippage(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "EXEC_QUALITY_MIN_FILLS_FOR_MEASURED", 3)
    led = _ledger(tmp_path)
    led.record_fill("AAA", "buy", "entry", 100.0, 100.10, 10)    # +10 bps
    led.record_fill("AAA", "sell", "target", 50.0, 49.95, 3)     # +10 bps
    led.record_fill("BBB", "buy", "entry", 200.0, 200.20, 5)     # +10 bps
    s = led.summary()
    assert s["n_fills"] == 3
    assert s["avg_slippage_bps"] == pytest.approx(10.0)
    assert s["measured_slippage_pct"] == pytest.approx(0.001)   # 10 bps


def test_measured_slippage_needs_min_fills(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "EXEC_QUALITY_MIN_FILLS_FOR_MEASURED", 10)
    led = _ledger(tmp_path)
    led.record_fill("AAA", "buy", "entry", 100.0, 100.10, 10)
    assert led.measured_slippage_pct() is None  # too few fills


def test_measured_slippage_floored_at_zero(tmp_path, monkeypatch):
    # Favorable execution (negative avg slippage) must not become a cost subsidy.
    monkeypatch.setattr(cfg, "EXEC_QUALITY_MIN_FILLS_FOR_MEASURED", 2)
    led = _ledger(tmp_path)
    led.record_fill("AAA", "buy", "entry", 100.0, 99.80, 10)   # -20 bps (favorable)
    led.record_fill("BBB", "buy", "entry", 100.0, 99.90, 10)   # -10 bps (favorable)
    assert led.measured_slippage_pct() == 0.0


def test_empty_summary(tmp_path):
    assert _ledger(tmp_path).summary()["n_fills"] == 0


def test_malformed_ledger_file_is_tolerated(tmp_path):
    p = tmp_path / "eq.json"
    p.write_text("not json", encoding="utf-8")
    led = ExecutionQualityLedger(path=str(p)).load()
    assert led.fills() == []


# ── executor wiring + backtest feedback ───────────────────────────────────────

def test_executor_helper_records_to_default_ledger(monkeypatch):
    # conftest points the default ledger at a temp path and resets the singleton.
    import src.alpaca.executor as ex
    from src.alpaca.execution_quality import default_ledger

    ex._record_execution_quality("AAA", "buy", "entry", 100.0, 100.30, 10, order=None)
    fills = default_ledger().fills()
    assert len(fills) == 1 and fills[0]["slippage_bps"] == pytest.approx(30.0)


def test_executor_helper_never_raises(monkeypatch):
    import src.alpaca.executor as ex
    # Even with garbage inputs the accounting must not break execution.
    ex._record_execution_quality("AAA", "buy", "entry", None, None, None, order=None)


def test_backtest_uses_measured_slippage_when_enabled(monkeypatch):
    import src.alpaca.execution_quality as eq
    from src.backtest import engine

    monkeypatch.setattr(cfg, "EXEC_QUALITY_USE_MEASURED_SLIPPAGE", True)
    monkeypatch.setattr(eq, "measured_slippage_pct", lambda: 0.005)
    assert engine._resolve_slippage() == pytest.approx(0.005)


def test_backtest_falls_back_to_static_slippage(monkeypatch):
    import src.alpaca.execution_quality as eq
    from src.backtest import engine

    monkeypatch.setattr(cfg, "EXEC_QUALITY_USE_MEASURED_SLIPPAGE", True)
    monkeypatch.setattr(eq, "measured_slippage_pct", lambda: None)  # too few fills
    assert engine._resolve_slippage() == engine.SLIPPAGE_PCT
    # And disabled → always static.
    monkeypatch.setattr(cfg, "EXEC_QUALITY_USE_MEASURED_SLIPPAGE", False)
    assert engine._resolve_slippage() == engine.SLIPPAGE_PCT
