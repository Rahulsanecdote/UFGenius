"""Tests for the edge-validation harness (upgrade plan P0.1).

The heavy engine is not exercised here — validation composes on top of it via an
injectable ``run_backtest`` callable, so these tests use deterministic fakes and
synthetic trade/return series. Everything is seeded and offline.
"""

from __future__ import annotations

import numpy as np

from src.backtest import validation
from src.backtest.validation import (
    bootstrap_return_metrics,
    bootstrap_trade_metrics,
    rolling_windows,
    validate_strategy,
    walk_forward,
    _split_oos,
)


# ── window splitting ─────────────────────────────────────────────────────────

def test_rolling_windows_are_contiguous_and_nonoverlapping():
    wins = rolling_windows("2020-01-01", "2020-12-31", 4)
    assert len(wins) == 4
    # each window's start is after the previous window's end (no overlap)
    for (_, prev_end), (next_start, _) in zip(wins, wins[1:]):
        assert next_start > prev_end
    assert wins[0][0] == "2020-01-01"
    assert wins[-1][1] == "2020-12-31"


def test_split_oos_holds_out_the_tail():
    is_start, is_end, oos_start, oos_end = _split_oos("2020-01-01", "2021-01-01", 0.30)
    assert is_start == "2020-01-01"
    assert oos_end == "2021-01-01"
    assert oos_start > is_end  # OOS begins after in-sample ends
    # ~30% of the ~366-day span is held out
    import pandas as pd
    total = (pd.Timestamp("2021-01-01") - pd.Timestamp("2020-01-01")).days
    held = (pd.Timestamp("2021-01-01") - pd.Timestamp(oos_start)).days
    assert 0.25 * total <= held <= 0.35 * total


# ── bootstrap ────────────────────────────────────────────────────────────────

def test_bootstrap_trade_metrics_is_deterministic_and_flags_a_winning_book():
    pnls = [100.0] * 14 + [-50.0] * 6  # 70% win, strongly positive expectancy
    a = bootstrap_trade_metrics(pnls, 10_000, n_resamples=500, seed=7)
    b = bootstrap_trade_metrics(pnls, 10_000, n_resamples=500, seed=7)
    assert a == b  # same seed → identical output
    assert a["prob_profitable"] > 0.95
    assert a["total_return_pct"]["p05"] > 0  # even the pessimistic tail is green
    assert a["profit_factor"]["p50"] > 1.0


def test_bootstrap_trade_metrics_flags_a_losing_book():
    pnls = [50.0] * 5 + [-100.0] * 15  # negative expectancy
    out = bootstrap_trade_metrics(pnls, 10_000, n_resamples=500, seed=3)
    assert out["prob_profitable"] < 0.10
    assert out["total_return_pct"]["p95"] < 0  # even the optimistic tail is red


def test_bootstrap_trade_metrics_handles_no_trades():
    out = bootstrap_trade_metrics([], 10_000)
    assert out["n_trades"] == 0


def test_bootstrap_return_metrics_sharpe_ci_positive_for_upward_drift():
    rng = np.random.default_rng(0)
    rets = 0.002 + 0.005 * rng.standard_normal(250)  # positive drift, real variance
    out = bootstrap_return_metrics(rets, n_resamples=500, block_size=5, seed=1)
    assert out["sharpe_ratio"]["p05"] is not None
    assert out["sharpe_ratio"]["p50"] > 0


def test_bootstrap_return_metrics_handles_short_series():
    assert "note" in bootstrap_return_metrics([0.01], n_resamples=10)


# ── walk-forward ─────────────────────────────────────────────────────────────

def _result(sharpe, ret, accept, trades=10):
    return {
        "sharpe_ratio": sharpe, "total_return_pct": ret, "win_rate_pct": 55.0,
        "profit_factor": 1.5, "max_drawdown_pct": -8.0, "total_trades": trades,
        "minimum_acceptance": {"all_pass": accept},
    }


def test_walk_forward_summarizes_per_window_stability():
    # Two profitable windows, two losing — persistence fraction should be 0.5.
    def fake(tickers, start, end, initial_capital=10_000, **kw):
        good = start < "2020-07-01"
        return _result(1.5 if good else -0.5, 5.0 if good else -4.0, good)

    wf = walk_forward(["AAA"], "2020-01-01", "2020-12-31", n_windows=4, run_backtest=fake)
    assert wf["stability"]["n_windows_with_trades"] == 4
    assert wf["stability"]["windows_profitable_fraction"] == 0.5
    assert wf["stability"]["sharpe_min"] == -0.5


def test_walk_forward_ignores_windows_with_no_trades():
    def fake(tickers, start, end, initial_capital=10_000, **kw):
        return _result(1.5, 5.0, True, trades=0)  # no trades anywhere

    wf = walk_forward(["AAA"], "2020-01-01", "2020-12-31", n_windows=3, run_backtest=fake)
    assert wf["stability"]["n_windows_with_trades"] == 0
    assert wf["stability"]["windows_profitable_fraction"] == 0.0


# ── orchestrator verdict ─────────────────────────────────────────────────────

def _strong(tickers, start, end, initial_capital=10_000, **kw):
    res = _result(1.8, 12.0, True, trades=20)
    res["trades"] = [{"pnl": 100.0}] * 14 + [{"pnl": -50.0}] * 6
    # rising equity with genuine variance → positive-drift Sharpe CI
    vals, v = [], 10_000.0
    for i in range(80):
        v *= 1 + (0.03 if i % 2 else 0.008)
        vals.append({"portfolio_value": round(v, 2)})
    res["equity_curve"] = vals
    return res


def _weak(tickers, start, end, initial_capital=10_000, **kw):
    res = _result(0.2, -6.0, False, trades=20)
    res["trades"] = [{"pnl": 40.0}] * 6 + [{"pnl": -90.0}] * 14
    vals, v = [], 10_000.0
    for i in range(80):
        v *= 1 - (0.02 if i % 2 else 0.005)
        vals.append({"portfolio_value": round(v, 2)})
    res["equity_curve"] = vals
    return res


def test_validate_strategy_passes_a_strong_edge_out_of_sample():
    out = validate_strategy(
        ["AAA"], "2020-01-01", "2021-01-01",
        n_windows=3, n_bootstrap=400, run_backtest=_strong,
    )
    assert out["verdict"]["validated"] is True
    assert all(out["verdict"]["checks"].values())
    assert "VALIDATED" in out["verdict"]["summary"]
    # the OOS split is genuinely held out
    assert out["split"]["out_of_sample"] > out["split"]["in_sample"]


def test_validate_strategy_rejects_a_weak_edge():
    out = validate_strategy(
        ["AAA"], "2020-01-01", "2021-01-01",
        n_windows=3, n_bootstrap=400, run_backtest=_weak,
    )
    assert out["verdict"]["validated"] is False
    assert "Do NOT deploy capital" in out["verdict"]["summary"]


def test_validate_strategy_is_reproducible_with_a_seed():
    kw = dict(n_windows=3, n_bootstrap=300, run_backtest=_strong, seed=99)
    a = validate_strategy(["AAA"], "2020-01-01", "2021-01-01", **kw)
    b = validate_strategy(["AAA"], "2020-01-01", "2021-01-01", **kw)
    assert a["bootstrap_out_of_sample"] == b["bootstrap_out_of_sample"]


def test_validate_strategy_rejects_bad_oos_fraction():
    import pytest
    with pytest.raises(ValueError):
        validate_strategy(["AAA"], "2020-01-01", "2021-01-01", oos_fraction=0.99)
