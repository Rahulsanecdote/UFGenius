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
    assert "equal-length" in plan["error"]


def test_exit_pcts_must_sum_to_100(monkeypatch):
    monkeypatch.setattr(cfg, "TARGET_RR_RATIOS", [1.5, 2.5, 4.0])
    monkeypatch.setattr(cfg, "TARGET_EXIT_PCTS", [30, 40, 20])  # 90
    plan = generate_trade_plan("AAPL", _signal(), account_size=50_000, df=_df())
    assert "sum to 100" in plan["error"]


def test_single_target_config_no_longer_crashes(monkeypatch):
    # The resistance snap indexed raw_targets[1] unconditionally — a 1-entry
    # config raised IndexError before validation existed.
    monkeypatch.setattr(cfg, "TARGET_RR_RATIOS", [2.0])
    monkeypatch.setattr(cfg, "TARGET_EXIT_PCTS", [100])
    plan = generate_trade_plan("AAPL", _signal(), account_size=50_000, df=_df())
    assert "error" not in plan
    assert list(plan["targets"].keys()) == ["T1"]
    assert plan["targets"]["T1"]["exit_pct"] == 100


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
