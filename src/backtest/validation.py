"""
Edge-validation harness for the backtest engine (upgrade plan P0.1).

The backtest engine reports metrics for a single run; this module answers the
harder question the upgrade plan cares about: *is the measured edge real, or a
lucky in-sample artifact?* It adds three things on top of
``backtest_signal_system`` without changing it:

1. **Held-out out-of-sample (OOS) split** — the final ``oos_fraction`` of the
   date range is never used for anything but the final gate, so acceptance is
   judged on data the strategy config was not chosen against.
2. **Walk-forward (rolling OOS) evaluation** — the in-sample span is cut into
   contiguous windows and the *fixed* strategy is run on each, so we can see
   whether the edge persists across time or lives in one window. (Per-window
   parameter refitting is P0.2; this is the scaffold for it — the fixed-config
   run is the degenerate "no refit" case.)
3. **Bootstrap / monte-carlo confidence intervals** — trade P&Ls and daily
   returns are resampled to produce CIs on total return, profit factor, win
   rate, max drawdown, and Sharpe, plus ``prob_profitable``. A point estimate
   with no interval cannot tell noise from edge; this can.

Nothing here assumes a strategy is profitable. A failed verdict is a valid,
useful outcome: it means **do not deploy capital**, not "add more features".
Determinism: every resampler takes a ``seed`` and defaults to a fixed one, so
runs and tests are reproducible.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from src.backtest.engine import RISK_FREE_RATE_ANNUAL, backtest_signal_system
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_TRADING_DAYS = 252
DEFAULT_SEED = 12345

# A validated edge must clear these on the HELD-OUT OOS data, not in-sample.
# Deliberately conservative — the point is to reject noise, not to flatter.
# Thresholds live in config.yaml (`validation:`) per the repo convention;
# these module names are read-throughs so the logic stays readable.
OOS_SHARPE_FLOOR = config.VALIDATION_OOS_SHARPE_FLOOR
BOOTSTRAP_SHARPE_P05_FLOOR = config.VALIDATION_BOOTSTRAP_SHARPE_P05_FLOOR
PROB_PROFITABLE_FLOOR = config.VALIDATION_PROB_PROFITABLE_FLOOR
WINDOW_PROFITABLE_FRACTION_FLOOR = config.VALIDATION_WINDOW_PROFITABLE_FRACTION_FLOOR
# A verdict on a thin sample is meaningless — resampling one winning trade makes
# prob_profitable 1.0. Require a minimum OOS sample before trusting the verdict.
MIN_OOS_TRADES = config.VALIDATION_MIN_OOS_TRADES
MIN_OOS_DAYS = config.VALIDATION_MIN_OOS_DAYS


# ── helpers ──────────────────────────────────────────────────────────────────

def _trade_pnls(result: dict) -> np.ndarray:
    """Extract the closed-trade P&L series from an engine result dict."""
    trades = result.get("trades") or []
    return np.array(
        [float(t["pnl"]) for t in trades if t.get("pnl") is not None],
        dtype=float,
    )


def _daily_returns(result: dict) -> np.ndarray:
    """Extract the daily portfolio return series from an engine equity curve."""
    curve = result.get("equity_curve") or []
    # Test for None, not truthiness: a legitimate portfolio value of exactly 0.0
    # must be kept, or the surrounding points get differenced across the gap.
    values = np.array(
        [float(p["portfolio_value"]) for p in curve if p.get("portfolio_value") is not None],
        dtype=float,
    )
    if values.size < 2:
        return np.array([], dtype=float)
    # A zero prior value yields inf/nan; that seam is intentionally dropped by
    # the finite filter below (so silence the expected divide warning).
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(values) / values[:-1]
    return rets[np.isfinite(rets)]


def _pct_ci(samples: np.ndarray) -> dict:
    """Mean + 5/50/95 percentile summary of a bootstrap sample distribution."""
    finite = samples[np.isfinite(samples)]
    if finite.size == 0:
        return {"mean": None, "p05": None, "p50": None, "p95": None, "n": 0}
    return {
        "mean": round(float(np.mean(finite)), 3),
        "p05": round(float(np.percentile(finite, 5)), 3),
        "p50": round(float(np.percentile(finite, 50)), 3),
        "p95": round(float(np.percentile(finite, 95)), 3),
        "n": int(finite.size),
    }


def _max_drawdown_pct(equity_path: np.ndarray) -> float:
    """Max drawdown (%, negative) of a cumulative equity path."""
    if equity_path.size == 0:
        return 0.0
    running_max = np.maximum.accumulate(equity_path)
    # Guard against a non-positive running max (equity wiped out).
    safe = np.where(running_max > 0, running_max, np.nan)
    dd = (equity_path - running_max) / safe
    dd = dd[np.isfinite(dd)]
    return float(np.min(dd) * 100) if dd.size else 0.0


def _moving_block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Moving-block-bootstrap index vector of length ~n (preserves autocorr)."""
    if n <= 0:
        return np.array([], dtype=int)
    block = max(1, min(block, n))
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=n_blocks)
    idx = np.concatenate([np.arange(s, s + block) for s in starts])
    return idx[:n]


# ── bootstrap / monte-carlo ──────────────────────────────────────────────────

def bootstrap_trade_metrics(
    trades: Any,
    initial_capital: float,
    *,
    n_resamples: int = 1000,
    block_size: int = 1,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Resample closed-trade P&Ls to get CIs on trade-level outcomes.

    ``trades`` may be an engine result dict or a raw P&L array/list. For each
    resample the trades are drawn with replacement (moving blocks when
    ``block_size > 1`` to keep streaks intact), an equity path is rebuilt in the
    resampled order, and total-return / profit-factor / win-rate / max-drawdown
    are recomputed. ``prob_profitable`` is the fraction of resamples that ended
    up net positive — the headline robustness number.
    """
    pnls = _trade_pnls(trades) if isinstance(trades, dict) else np.asarray(trades, dtype=float)
    n = pnls.size
    if n == 0:
        return {"n_trades": 0, "note": "no closed trades to bootstrap"}

    rng = np.random.default_rng(seed)
    total_return, profit_factor, win_rate, max_dd = (
        np.empty(n_resamples) for _ in range(4)
    )
    for i in range(n_resamples):
        idx = (
            _moving_block_indices(n, block_size, rng)
            if block_size > 1
            else rng.integers(0, n, size=n)
        )
        sample = pnls[idx]
        gross_profit = sample[sample > 0].sum()
        gross_loss = -sample[sample <= 0].sum()
        total_return[i] = sample.sum() / initial_capital * 100 if initial_capital else np.nan
        profit_factor[i] = (gross_profit / gross_loss) if gross_loss > 0 else np.nan
        win_rate[i] = (sample > 0).mean() * 100
        # Start the equity path at initial_capital (the true opening peak) so a
        # first-trade loss is counted in the drawdown, not hidden by treating the
        # post-first-trade balance as the peak.
        equity_path = np.concatenate(([initial_capital], initial_capital + np.cumsum(sample)))
        max_dd[i] = _max_drawdown_pct(equity_path)

    prob_profitable = float((total_return > 0).mean())
    return {
        "n_trades": int(n),
        "n_resamples": int(n_resamples),
        "block_size": int(block_size),
        "prob_profitable": round(prob_profitable, 3),
        "total_return_pct": _pct_ci(total_return),
        "profit_factor": _pct_ci(profit_factor),
        "win_rate_pct": _pct_ci(win_rate),
        "max_drawdown_pct": _pct_ci(max_dd),
    }


def bootstrap_return_metrics(
    equity: Any,
    *,
    n_resamples: int = 1000,
    block_size: int = 5,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Block-bootstrap the daily return series to get a Sharpe/Sortino CI.

    ``equity`` may be an engine result dict (its ``equity_curve`` is used) or a
    raw daily-return array. Block bootstrap (default block 5 ≈ one trading week)
    preserves short-horizon autocorrelation so the Sharpe interval is not
    artificially tight. Annualized with the engine's own risk-free rate and
    252-day convention so it is comparable to the point metrics.
    """
    rets = _daily_returns(equity) if isinstance(equity, dict) else np.asarray(equity, dtype=float)
    n = rets.size
    if n < 2:
        return {"n_days": int(n), "note": "insufficient return history to bootstrap"}

    rf_daily = RISK_FREE_RATE_ANNUAL / _TRADING_DAYS
    rng = np.random.default_rng(seed)
    sharpe = np.empty(n_resamples)
    sortino = np.empty(n_resamples)
    annual_return = np.empty(n_resamples)
    for i in range(n_resamples):
        sample = rets[_moving_block_indices(n, block_size, rng)]
        sd = sample.std()
        excess = sample.mean() - rf_daily
        sharpe[i] = (excess / sd * np.sqrt(_TRADING_DAYS)) if sd > 0 else np.nan
        neg = sample[sample < 0]
        # No losing days in this resample → downside deviation is undefined
        # (avoid a std() over an empty slice, which warns and returns nan).
        downside = neg.std() if neg.size else 0.0
        sortino[i] = (excess / downside * np.sqrt(_TRADING_DAYS)) if downside > 0 else np.nan
        annual_return[i] = ((1 + sample.mean()) ** _TRADING_DAYS - 1) * 100

    return {
        "n_days": int(n),
        "n_resamples": int(n_resamples),
        "block_size": int(block_size),
        "sharpe_ratio": _pct_ci(sharpe),
        "sortino_ratio": _pct_ci(sortino),
        "annual_return_pct": _pct_ci(annual_return),
    }


# ── walk-forward ─────────────────────────────────────────────────────────────

def rolling_windows(start: str, end: str, n_windows: int) -> list[tuple[str, str]]:
    """Split [start, end] into ``n_windows`` contiguous, non-overlapping spans."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if end_ts <= start_ts or n_windows < 1:
        return [(start, end)]
    edges = pd.date_range(start_ts, end_ts, periods=n_windows + 1)
    out: list[tuple[str, str]] = []
    for i in range(n_windows):
        w_start = edges[i] if i == 0 else edges[i] + pd.Timedelta(days=1)
        out.append((w_start.strftime("%Y-%m-%d"), edges[i + 1].strftime("%Y-%m-%d")))
    return out


def walk_forward(
    tickers: list[str],
    start: str,
    end: str,
    *,
    n_windows: int = 4,
    initial_capital: float = 10_000,
    run_backtest: Callable[..., dict] = backtest_signal_system,
    **backtest_kwargs: Any,
) -> dict:
    """Run the (fixed) strategy across sequential windows and summarize stability.

    ``run_backtest`` is injectable so this is testable without a live engine run
    and so P0.2 can pass a refit-then-apply wrapper later. Returns each window's
    headline metrics plus a stability block: how consistent Sharpe/return are
    across windows and what fraction of windows were profitable and passed the
    minimum-acceptance gate.
    """
    windows = rolling_windows(start, end, n_windows)
    per_window: list[dict] = []
    for w_start, w_end in windows:
        result = run_backtest(tickers, w_start, w_end, initial_capital=initial_capital, **backtest_kwargs)
        per_window.append({
            "period": f"{w_start} → {w_end}",
            "sharpe_ratio": result.get("sharpe_ratio"),
            "total_return_pct": result.get("total_return_pct"),
            "win_rate_pct": result.get("win_rate_pct"),
            "profit_factor": result.get("profit_factor"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "total_trades": result.get("total_trades", 0),
            "acceptance_pass": bool((result.get("minimum_acceptance") or {}).get("all_pass")),
        })

    evaluated = [w for w in per_window if (w["total_trades"] or 0) > 0]
    sharpes = np.array([w["sharpe_ratio"] for w in evaluated if w["sharpe_ratio"] is not None], dtype=float)
    returns = np.array([w["total_return_pct"] for w in evaluated if w["total_return_pct"] is not None], dtype=float)
    n_eval = len(evaluated)
    profitable = sum(1 for w in evaluated if (w["total_return_pct"] or 0) > 0)

    stability = {
        "n_windows": len(windows),
        "n_windows_with_trades": n_eval,
        "windows_profitable": profitable,
        # Denominator is ALL configured windows, not just windows that traded —
        # a strategy that fires in one of four windows has NOT demonstrated
        # persistence, even if that lone window was profitable.
        "windows_profitable_fraction": round(profitable / len(windows), 3) if windows else 0.0,
        "windows_accepted": sum(1 for w in evaluated if w["acceptance_pass"]),
        "sharpe_mean": round(float(sharpes.mean()), 3) if sharpes.size else None,
        "sharpe_std": round(float(sharpes.std()), 3) if sharpes.size else None,
        "sharpe_min": round(float(sharpes.min()), 3) if sharpes.size else None,
        "return_mean_pct": round(float(returns.mean()), 3) if returns.size else None,
        "return_min_pct": round(float(returns.min()), 3) if returns.size else None,
    }
    return {"windows": per_window, "stability": stability}


# ── orchestrator ─────────────────────────────────────────────────────────────

def _split_oos(start: str, end: str, oos_fraction: float) -> tuple[str, str, str, str]:
    """Return (is_start, is_end, oos_start, oos_end) held-out split by time."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    total_days = (end_ts - start_ts).days
    if total_days < 2:
        raise ValueError(f"date range {start}→{end} is too short to hold out an OOS split")
    # Clamp so the split is never degenerate/inverted: at least 1 day each side.
    oos_days = max(1, min(total_days - 1, round(total_days * oos_fraction)))
    boundary = end_ts - pd.Timedelta(days=oos_days)
    return (
        start_ts.strftime("%Y-%m-%d"),
        boundary.strftime("%Y-%m-%d"),
        (boundary + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        end_ts.strftime("%Y-%m-%d"),
    )


def validate_strategy(
    tickers: list[str],
    start: str,
    end: str,
    *,
    initial_capital: float = 10_000,
    n_windows: int = 4,
    n_bootstrap: int = 1000,
    oos_fraction: float = 0.30,
    seed: int = DEFAULT_SEED,
    run_backtest: Callable[..., dict] = backtest_signal_system,
    **backtest_kwargs: Any,
) -> dict:
    """Full P0.1 validation: walk-forward on in-sample + held-out OOS + bootstrap.

    Returns a structured report whose ``verdict.validated`` is True only when the
    edge survives ALL of: OOS point Sharpe ≥ floor, OOS minimum-acceptance gate,
    bootstrap 5th-pct Sharpe > 0, ``prob_profitable`` ≥ floor, and persistence
    across a majority of walk-forward windows. A False verdict means the edge is
    not demonstrated out-of-sample — do not deploy capital.
    """
    if not 0.05 <= oos_fraction <= 0.9:
        raise ValueError("oos_fraction must be between 0.05 and 0.9")

    is_start, is_end, oos_start, oos_end = _split_oos(start, end, oos_fraction)
    log.info(f"Validation split — in-sample {is_start}→{is_end}, held-out OOS {oos_start}→{oos_end}")

    wf = walk_forward(
        tickers, is_start, is_end,
        n_windows=n_windows, initial_capital=initial_capital,
        run_backtest=run_backtest, **backtest_kwargs,
    )

    oos = run_backtest(tickers, oos_start, oos_end, initial_capital=initial_capital, **backtest_kwargs)
    oos_trade_ci = bootstrap_trade_metrics(oos, initial_capital, n_resamples=n_bootstrap, seed=seed)
    oos_return_ci = bootstrap_return_metrics(oos, n_resamples=n_bootstrap, seed=seed)

    oos_sharpe = oos.get("sharpe_ratio")
    oos_accept = bool((oos.get("minimum_acceptance") or {}).get("all_pass"))
    boot_sharpe_p05 = (oos_return_ci.get("sharpe_ratio") or {}).get("p05")
    prob_profitable = oos_trade_ci.get("prob_profitable")
    windows_frac = wf["stability"]["windows_profitable_fraction"]
    oos_trades = int(oos.get("total_trades", 0) or 0)
    oos_days = max(0, len(oos.get("equity_curve") or []) - 1)

    checks = {
        # Gate on sample size FIRST — a verdict on a handful of trades is noise,
        # and resampling one winning trade would otherwise show prob_profitable 1.0.
        "sufficient_sample_ok": oos_trades >= MIN_OOS_TRADES and oos_days >= MIN_OOS_DAYS,
        "oos_sharpe_ok": oos_sharpe is not None and oos_sharpe >= OOS_SHARPE_FLOOR,
        "oos_acceptance_ok": oos_accept,
        "bootstrap_sharpe_p05_ok": boot_sharpe_p05 is not None and boot_sharpe_p05 > BOOTSTRAP_SHARPE_P05_FLOOR,
        "prob_profitable_ok": prob_profitable is not None and prob_profitable >= PROB_PROFITABLE_FLOOR,
        "walkforward_persistence_ok": windows_frac >= WINDOW_PROFITABLE_FRACTION_FLOOR,
    }
    validated = all(checks.values())

    return {
        "split": {
            "in_sample": f"{is_start} → {is_end}",
            "out_of_sample": f"{oos_start} → {oos_end}",
            "oos_fraction": oos_fraction,
        },
        "walk_forward": wf,
        "out_of_sample": {
            "sharpe_ratio": oos_sharpe,
            "total_return_pct": oos.get("total_return_pct"),
            "max_drawdown_pct": oos.get("max_drawdown_pct"),
            "win_rate_pct": oos.get("win_rate_pct"),
            "profit_factor": oos.get("profit_factor"),
            "total_trades": oos.get("total_trades", 0),
            "minimum_acceptance": oos.get("minimum_acceptance"),
            "bias_disclosures": oos.get("bias_disclosures"),
        },
        "bootstrap_out_of_sample": {
            "trade_level": oos_trade_ci,
            "return_level": oos_return_ci,
            "seed": seed,
        },
        "verdict": {
            "validated": validated,
            "checks": checks,
            "summary": (
                "VALIDATED (out-of-sample): edge persists across walk-forward "
                "windows and survives bootstrap CIs. Still paper-trade before "
                "risking capital."
                if validated else
                "NOT VALIDATED: the edge is not demonstrated out-of-sample. "
                "Do NOT deploy capital — revisit the strategy, not the report."
            ),
            "disclaimer": (
                "Validation reduces the risk of deploying a noise-driven strategy; "
                "it does not guarantee future profitability. Past performance is "
                "not indicative of future results."
            ),
        },
    }
