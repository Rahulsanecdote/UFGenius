"""
Parameter-selection discipline for the backtest (upgrade plan P0.2).

The danger with tuning strategy knobs is picking the combination that looked
best *in sample* — curve-fitting noise. This module enforces the discipline the
plan asks for:

1. **Selection uses only in-sample (validation) data.** The full range is split
   once into in-sample and a held-out out-of-sample tail; every candidate is
   scored by walk-forward *within the in-sample span only*. The OOS tail is
   never seen during selection.
2. **Every candidate is a real backtest.** Candidates are `StrategyParams`
   activated via `engine.strategy_params(...)`, so the search actually varies
   entry band / stop / target / sizing — not cosmetic knobs.
3. **An explicit overfitting haircut.** Searching N combinations inflates the
   best Sharpe even with zero true skill. We compute the Bailey & López de Prado
   *expected maximum Sharpe under the null* for N trials and flag the winner as
   spurious unless it clears that threshold — and then re-check it on the
   untouched OOS tail.

Nothing here assumes profitability. If the winner does not beat the
false-strategy threshold and validate out-of-sample, the honest outcome is
**do not deploy** — the search found noise, not an edge.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from statistics import NormalDist
from typing import Any, Callable

import numpy as np

from src.backtest.engine import StrategyParams, backtest_signal_system, strategy_params
from src.backtest.validation import (
    BOOTSTRAP_SHARPE_P05_FLOOR,
    MIN_OOS_DAYS,
    MIN_OOS_TRADES,
    OOS_SHARPE_FLOOR,
    PROB_PROFITABLE_FLOOR,
    bootstrap_return_metrics,
    bootstrap_trade_metrics,
    walk_forward,
    _split_oos,
)
from src.utils.logger import get_logger

log = get_logger(__name__)

_EULER_MASCHERONI = 0.5772156649015329
# Refuse to run an unbounded grid — a search that large is itself an overfitting
# red flag, and the false-strategy threshold blows up with N.
MAX_CANDIDATES = 200


# ── candidate generation ─────────────────────────────────────────────────────

def grid_candidates(param_grid: dict[str, list], *, base: StrategyParams | None = None) -> list[StrategyParams]:
    """Expand a ``{field: [values...]}`` grid into StrategyParams candidates.

    Cartesian product over the listed fields; unlisted fields keep ``base``
    (defaults). Raises if the grid exceeds ``MAX_CANDIDATES``.
    """
    base = base or StrategyParams()
    if not param_grid:
        return [base]
    fields = list(param_grid)
    value_lists = [param_grid[f] for f in fields]
    combos = list(itertools.product(*value_lists))
    if len(combos) > MAX_CANDIDATES:
        raise ValueError(
            f"grid expands to {len(combos)} candidates (> {MAX_CANDIDATES}); "
            "narrow it — a search this wide overfits by construction"
        )
    return [replace(base, **dict(zip(fields, combo))) for combo in combos]


# ── multiple-testing haircut ─────────────────────────────────────────────────

def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Expected maximum Sharpe from ``n_trials`` skill-less trials (BLdP 2014).

    E[max SR] ≈ σ · [ (1-γ)·Z⁻¹(1 - 1/N) + γ·Z⁻¹(1 - 1/(N·e)) ], where σ is the
    cross-trial Sharpe dispersion and γ is the Euler–Mascheroni constant. If the
    best trial's Sharpe does not exceed this, the "edge" is consistent with
    multiple-testing luck. An approximation (assumes roughly independent,
    ~normal trial Sharpes) — a screen, not a proof.
    """
    if n_trials < 2 or sharpe_std <= 0:
        return 0.0
    nd = NormalDist()
    z1 = nd.inv_cdf(1 - 1.0 / n_trials)
    z2 = nd.inv_cdf(1 - 1.0 / (n_trials * np.e))
    return float(sharpe_std * ((1 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2))


# ── in-sample scoring + OOS confirmation ─────────────────────────────────────

def _score_candidate(
    params: StrategyParams,
    tickers: list[str],
    is_start: str,
    is_end: str,
    *,
    n_windows: int,
    initial_capital: float,
    run_backtest: Callable[..., dict],
    **bt_kwargs: Any,
) -> dict:
    """Walk-forward score of one candidate on the IN-SAMPLE span only."""
    def _run(tk, start, end, **kw):
        with strategy_params(params):
            return run_backtest(tk, start, end, **kw)

    wf = walk_forward(
        tickers, is_start, is_end,
        n_windows=n_windows, initial_capital=initial_capital,
        run_backtest=_run, **bt_kwargs,
    )
    st = wf["stability"]
    return {
        "sharpe_mean": st["sharpe_mean"],
        "windows_profitable_fraction": st["windows_profitable_fraction"],
        "windows_with_trades": st["n_windows_with_trades"],
    }


def _evaluate_oos(
    params: StrategyParams,
    tickers: list[str],
    oos_start: str,
    oos_end: str,
    *,
    initial_capital: float,
    n_bootstrap: int,
    seed: int,
    run_backtest: Callable[..., dict],
    **bt_kwargs: Any,
) -> dict:
    """Confirm the selected candidate on the HELD-OUT OOS tail (never tuned on)."""
    with strategy_params(params):
        oos = run_backtest(tickers, oos_start, oos_end, initial_capital=initial_capital, **bt_kwargs)
    trade_ci = bootstrap_trade_metrics(oos, initial_capital, n_resamples=n_bootstrap, seed=seed)
    ret_ci = bootstrap_return_metrics(oos, n_resamples=n_bootstrap, seed=seed)

    oos_sharpe = oos.get("sharpe_ratio")
    boot_p05 = (ret_ci.get("sharpe_ratio") or {}).get("p05")
    prob_profitable = trade_ci.get("prob_profitable")
    oos_trades = int(oos.get("total_trades", 0) or 0)
    oos_days = max(0, len(oos.get("equity_curve") or []) - 1)

    checks = {
        "sufficient_sample_ok": oos_trades >= MIN_OOS_TRADES and oos_days >= MIN_OOS_DAYS,
        "oos_sharpe_ok": oos_sharpe is not None and oos_sharpe >= OOS_SHARPE_FLOOR,
        "bootstrap_sharpe_p05_ok": boot_p05 is not None and boot_p05 > BOOTSTRAP_SHARPE_P05_FLOOR,
        "prob_profitable_ok": prob_profitable is not None and prob_profitable >= PROB_PROFITABLE_FLOOR,
    }
    return {
        "sharpe_ratio": oos_sharpe,
        "total_return_pct": oos.get("total_return_pct"),
        "max_drawdown_pct": oos.get("max_drawdown_pct"),
        "total_trades": oos_trades,
        "prob_profitable": prob_profitable,
        "bootstrap_sharpe_p05": boot_p05,
        "checks": checks,
        "confirmed": all(checks.values()),
    }


def parameter_search(
    tickers: list[str],
    start: str,
    end: str,
    param_grid: dict[str, list],
    *,
    initial_capital: float = 10_000,
    n_windows: int = 4,
    n_bootstrap: int = 1000,
    oos_fraction: float = 0.30,
    seed: int = 12345,
    run_backtest: Callable[..., dict] = backtest_signal_system,
    **bt_kwargs: Any,
) -> dict:
    """Select strategy parameters with anti-overfitting discipline (P0.2).

    Scores every grid candidate by in-sample walk-forward, picks the best by
    mean fold Sharpe, applies the false-strategy (expected-max-Sharpe) haircut
    for the number of trials, then confirms the winner on the untouched OOS
    tail. ``selection.trustworthy`` is True only when the winner beats the
    multiple-testing threshold AND validates out-of-sample.
    """
    if not 0.05 <= oos_fraction <= 0.9:
        raise ValueError("oos_fraction must be between 0.05 and 0.9")

    candidates = grid_candidates(param_grid)
    is_start, is_end, oos_start, oos_end = _split_oos(start, end, oos_fraction)
    log.info(f"Parameter search: {len(candidates)} candidates, in-sample {is_start}→{is_end}")

    scored: list[dict] = []
    for params in candidates:
        score = _score_candidate(
            params, tickers, is_start, is_end,
            n_windows=n_windows, initial_capital=initial_capital,
            run_backtest=run_backtest, **bt_kwargs,
        )
        scored.append({"params": params, **score})

    # Rank by in-sample mean Sharpe; candidates that never traded are unrankable.
    rankable = [s for s in scored if s["sharpe_mean"] is not None]
    rankable.sort(key=lambda s: s["sharpe_mean"], reverse=True)

    sharpes = np.array([s["sharpe_mean"] for s in rankable], dtype=float)
    threshold = expected_max_sharpe(len(rankable), float(sharpes.std())) if sharpes.size > 1 else 0.0

    winner = rankable[0] if rankable else None
    beats_threshold = bool(winner and winner["sharpe_mean"] > threshold)

    oos_eval = None
    if winner is not None:
        oos_eval = _evaluate_oos(
            winner["params"], tickers, oos_start, oos_end,
            initial_capital=initial_capital, n_bootstrap=n_bootstrap, seed=seed,
            run_backtest=run_backtest, **bt_kwargs,
        )

    trustworthy = bool(beats_threshold and oos_eval and oos_eval["confirmed"])

    def _summ(s: dict) -> dict:
        p = s["params"]
        return {
            "params": {
                "entry_rsi_min": p.entry_rsi_min, "entry_rsi_max": p.entry_rsi_max,
                "atr_stop_mult": p.atr_stop_mult, "target_rr": list(p.target_rr),
                "risk_per_trade": p.risk_per_trade, "max_position_pct": p.max_position_pct,
            },
            "insample_sharpe_mean": s["sharpe_mean"],
            "insample_windows_profitable_fraction": s["windows_profitable_fraction"],
        }

    return {
        "split": {"in_sample": f"{is_start} → {is_end}", "out_of_sample": f"{oos_start} → {oos_end}"},
        "n_candidates": len(candidates),
        "n_rankable": len(rankable),
        "ranking": [_summ(s) for s in rankable[:10]],
        "false_strategy_threshold": round(threshold, 3),
        "selection": {
            "params": _summ(winner)["params"] if winner else None,
            "insample_sharpe_mean": winner["sharpe_mean"] if winner else None,
            "beats_false_strategy_threshold": beats_threshold,
            "out_of_sample": oos_eval,
            "trustworthy": trustworthy,
            "summary": (
                "TRUSTWORTHY: the selected parameters beat the multiple-testing "
                "threshold and validated on the held-out out-of-sample tail. Still "
                "paper-trade before risking capital."
                if trustworthy else
                "NOT TRUSTWORTHY: the best in-sample parameters are consistent with "
                "curve-fitting (did not beat the false-strategy threshold and/or did "
                "not validate out-of-sample). Do NOT deploy this configuration."
            ),
        },
        "disclaimer": (
            "In-sample selection inflates performance; this search screens for that "
            "but does not guarantee future results. Past performance is not indicative."
        ),
        "seed": seed,
    }


# A coarse, deliberately small default grid for `bot.py --mode optimize`.
# Kept tiny on purpose: a wide grid overfits and inflates the threshold.
DEFAULT_GRID: dict[str, list] = {
    "atr_stop_mult": [1.5, 2.0, 2.5],
    "entry_rsi_min": [40.0, 45.0, 50.0],
}
