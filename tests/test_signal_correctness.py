"""Tests for signal-correctness fixes (audit M3, M4-targets, M7)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import src.utils.config as cfg
from src.fundamental.scorer import _composite, _piotroski
from src.signals.trade_plan import generate_trade_plan


def _df(rows: int = 60, price: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D")
    close = np.linspace(price * 0.9, price, rows)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": [1_000_000] * rows,
        },
        index=dates,
    )


def _signal(label: str = "STRONG_BUY") -> dict:
    return {"signal": label, "score": 80.0, "confidence": "HIGH", "reasons": ["test"]}


# ── M3: long-only planner refuses SELL/HOLD ─────────────────────────────────

@pytest.mark.parametrize("label", ["SELL", "STRONG_SELL", "WEAK_SELL", "HOLD", "UNKNOWN"])
def test_non_buy_signals_get_skip_not_bullish_plan(label):
    plan = generate_trade_plan("AAPL", _signal(label), account_size=50_000, df=_df())
    assert plan.get("skip") is True
    assert "entry" not in plan  # no bullish geometry attached
    assert label in plan["reason"] or "Long-only" in plan["reason"]


@pytest.mark.parametrize("label", ["STRONG_BUY", "BUY", "WEAK_BUY"])
def test_buy_family_signals_still_get_full_plans(label):
    plan = generate_trade_plan("AAPL", _signal(label), account_size=50_000, df=_df())
    assert plan.get("skip") is not True
    assert "entry" in plan and "stop_loss" in plan and "targets" in plan


# ── M4: target config validation ────────────────────────────────────────────

def test_mismatched_target_lists_fail_loudly(monkeypatch):
    monkeypatch.setattr(cfg, "TARGET_RR_RATIOS", [1.5, 2.5])
    monkeypatch.setattr(cfg, "TARGET_EXIT_PCTS", [30, 40, 30])
    plan = generate_trade_plan("AAPL", _signal(), account_size=50_000, df=_df())
    assert "exactly 3" in plan["error"]


def test_exit_pcts_must_sum_to_100(monkeypatch):
    monkeypatch.setattr(cfg, "TARGET_RR_RATIOS", [1.5, 2.5, 4.0])
    monkeypatch.setattr(cfg, "TARGET_EXIT_PCTS", [30, 40, 20])  # 90
    plan = generate_trade_plan("AAPL", _signal(), account_size=50_000, df=_df())
    assert "sum to 100" in plan["error"]


def test_non_three_target_configs_rejected_loudly(monkeypatch):
    # Exactly 3 targets are supported end-to-end: PositionTracker/executor
    # allocate fixed T1/T2/T3 tranches and default MISSING targets to the
    # entry price — a 1-target config would sell ~70% at breakeven. Before
    # validation this crashed (IndexError at the resistance snap); now it is
    # a loud config error, never a fabricated breakeven exit.
    for ratios, pcts in ([([2.0]), ([100])], [([1.5, 2.5, 4.0, 6.0]), ([25, 25, 25, 25])]):
        monkeypatch.setattr(cfg, "TARGET_RR_RATIOS", ratios)
        monkeypatch.setattr(cfg, "TARGET_EXIT_PCTS", pcts)
        plan = generate_trade_plan("AAPL", _signal(), account_size=50_000, df=_df())
        assert "exactly 3" in plan["error"], f"{ratios}/{pcts}"


# ── M7: Piotroski normalized by measurable criteria ─────────────────────────

def _fundamentals():
    # Healthy company, but NO prior-period data → F3/F7 unmeasurable.
    return {
        "net_income": 100, "total_assets": 1000, "operating_cash_flow": 150,
        "total_debt": 200, "current_assets": 500, "current_liabilities": 250,
        "gross_profit": 400, "revenue": 900,
    }


def test_piotroski_marks_unmeasurable_criteria_none():
    score, detail = _piotroski(_fundamentals())
    assert detail["F3_roa_improving"] is None
    assert detail["F7_no_dilution"] is None
    assert score == 7  # all measurable criteria pass


def test_composite_normalizes_by_measurable_not_nine():
    score, detail = _piotroski(_fundamentals())
    measurable = sum(1 for v in detail.values() if v is not None)
    assert measurable == 7

    # A perfect 7/7 must earn the full 25 Piotroski points — dividing by a
    # fixed 9 capped every ticker at 19.4 (audit M7).
    full = _composite(score, None, {}, {}, measurable)
    old_style = _composite(score, None, {}, {}, 9)
    assert full > old_style
    assert full == pytest.approx(25, abs=1)  # only Piotroski contributes here


def test_composite_handles_zero_measurable():
    assert _composite(0, None, {}, {}, 0) == 0  # no division crash


def test_declining_roa_is_a_measured_failure_not_unavailable():
    # Prior-period data IS present and ROA declined: F3 must be False (a
    # failed criterion that counts in the denominator), not None — otherwise
    # a measured failure inflates the Piotroski component (6/7 vs 6/8).
    fd = _fundamentals()
    fd["net_income_prev"] = 200          # prior ROA 200/900 > current 100/1000
    fd["total_assets_prev"] = 900
    score, detail = _piotroski(fd)
    assert detail["F3_roa_improving"] is False
    measurable = sum(1 for v in detail.values() if v is not None)
    assert measurable == 8  # only F7 remains unmeasurable


def test_improving_roa_still_scores():
    fd = _fundamentals()
    fd["net_income_prev"] = 50           # prior ROA 50/1000 < current 100/1000
    fd["total_assets_prev"] = 1000
    score, detail = _piotroski(fd)
    assert detail["F3_roa_improving"] is True
    assert score == 8  # the 7 baseline passes + F3
