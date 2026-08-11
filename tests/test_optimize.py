"""Tests for the P0.2 parameter-selection harness.

Offline and deterministic: an injected ``run_backtest`` fake reads the active
``StrategyParams`` (which the search swaps in per candidate via the engine's
context manager), so different candidates get different scores without a live
backtest.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.backtest import engine
from src.backtest.engine import StrategyParams
from src.backtest.optimize import (
    MAX_CANDIDATES,
    expected_max_sharpe,
    grid_candidates,
    parameter_search,
)


# ── candidate generation ─────────────────────────────────────────────────────

def test_grid_candidates_is_the_cartesian_product():
    cands = grid_candidates({"atr_stop_mult": [1.5, 2.0], "entry_rsi_min": [40.0, 45.0, 50.0]})
    assert len(cands) == 6
    # unlisted fields keep defaults
    assert all(c.max_position_pct == StrategyParams().max_position_pct for c in cands)
    assert {c.atr_stop_mult for c in cands} == {1.5, 2.0}


def test_grid_candidates_rejects_an_oversized_grid():
    with pytest.raises(ValueError):
        grid_candidates({"atr_stop_mult": list(range(MAX_CANDIDATES + 1))})


# ── multiple-testing haircut ─────────────────────────────────────────────────

def test_expected_max_sharpe_properties():
    assert expected_max_sharpe(1, 0.5) == 0.0        # <2 trials → no haircut
    assert expected_max_sharpe(10, 0.0) == 0.0       # no dispersion → no haircut
    # increases with number of trials and with dispersion
    assert expected_max_sharpe(50, 0.5) > expected_max_sharpe(5, 0.5) > 0
    assert expected_max_sharpe(20, 1.0) > expected_max_sharpe(20, 0.4)


# ── parameter_search ─────────────────────────────────────────────────────────

def _fake(sharpe_by_mult: dict[float, float], *, confirm_mult: float | None):
    """Build a run_backtest fake keyed on the active atr_stop_mult.

    In-sample score = sharpe_by_mult[mult]. The OOS window (only reached for the
    selected winner) confirms iff mult == confirm_mult.
    """
    def fake(tickers, start, end, initial_capital=10_000, **kw):
        mult = engine._ACTIVE_PARAMS.atr_stop_mult
        sharpe = sharpe_by_mult.get(mult, 0.0)
        res = {
            "sharpe_ratio": sharpe, "total_return_pct": sharpe * 5, "win_rate_pct": 55.0,
            "profit_factor": 1.5, "max_drawdown_pct": -8.0, "total_trades": 25,
            "minimum_acceptance": {"all_pass": sharpe >= 1.0},
        }
        up = confirm_mult is not None and abs(mult - confirm_mult) < 1e-9
        res["trades"] = ([{"pnl": 100.0}] * 18 + [{"pnl": -50.0}] * 7) if up else [{"pnl": -30.0}] * 25
        vals, v = [], 10_000.0
        for i in range(80):
            v *= (1 + (0.03 if i % 2 else 0.008)) if up else (1 - (0.01 if i % 2 else 0.004))
            vals.append({"portfolio_value": round(v, 2)})
        res["equity_curve"] = vals
        return res
    return fake


_GRID = {"atr_stop_mult": [1.0, 1.5, 2.0, 2.5, 3.0]}


def test_search_selects_best_insample_and_confirms_oos():
    fake = _fake({1.0: 0.2, 1.5: 0.3, 2.0: 2.5, 2.5: 0.3, 3.0: 0.2}, confirm_mult=2.0)
    out = parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID,
                           n_windows=3, n_bootstrap=300, run_backtest=fake)
    sel = out["selection"]
    assert sel["params"]["atr_stop_mult"] == 2.0          # best in-sample picked
    assert sel["beats_false_strategy_threshold"] is True  # dominant winner clears haircut
    assert sel["out_of_sample"]["confirmed"] is True
    assert sel["trustworthy"] is True
    # reported flag is consistent with the reported threshold
    assert sel["beats_false_strategy_threshold"] == (
        sel["insample_sharpe_mean"] > out["false_strategy_threshold"]
    )


def test_search_not_trustworthy_when_oos_fails():
    # 2.0 wins in-sample and beats the haircut, but its OOS does not confirm.
    fake = _fake({1.0: 0.2, 1.5: 0.3, 2.0: 2.5, 2.5: 0.3, 3.0: 0.2}, confirm_mult=None)
    out = parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID,
                           n_windows=3, n_bootstrap=300, run_backtest=fake)
    sel = out["selection"]
    assert sel["params"]["atr_stop_mult"] == 2.0
    assert sel["out_of_sample"]["confirmed"] is False
    assert sel["trustworthy"] is False


def test_search_flags_curve_fitting_when_winner_is_within_the_null():
    # High dispersion, modest max → the best is consistent with multiple-testing
    # luck, so it must not be flagged trustworthy.
    fake = _fake({1.0: -1.5, 1.5: 1.0, 2.0: 1.05, 2.5: -1.2, 3.0: 0.9}, confirm_mult=2.0)
    out = parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID,
                           n_windows=3, n_bootstrap=200, run_backtest=fake)
    sel = out["selection"]
    assert sel["beats_false_strategy_threshold"] is False
    assert sel["trustworthy"] is False


def test_search_is_reproducible_with_a_seed():
    fake = _fake({1.0: 0.2, 1.5: 0.3, 2.0: 2.5, 2.5: 0.3, 3.0: 0.2}, confirm_mult=2.0)
    kw = dict(n_windows=3, n_bootstrap=200, run_backtest=fake, seed=7)
    a = parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID, **kw)
    b = parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID, **kw)
    assert a["selection"]["out_of_sample"] == b["selection"]["out_of_sample"]


def test_search_rejects_bad_oos_fraction():
    with pytest.raises(ValueError):
        parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID, oos_fraction=0.99)


def test_engine_params_restored_after_search():
    before = engine._ACTIVE_PARAMS
    fake = _fake({1.0: 0.2, 1.5: 0.3, 2.0: 2.5, 2.5: 0.3, 3.0: 0.2}, confirm_mult=2.0)
    parameter_search(["AAA"], "2020-01-01", "2021-01-01", _GRID,
                     n_windows=2, n_bootstrap=100, run_backtest=fake)
    assert engine._ACTIVE_PARAMS is before  # context manager restored the default
