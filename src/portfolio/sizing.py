"""Volatility- and correlation-aware position sizing (roadmap Phase 4).

Pure, deterministic numpy/pandas helpers — no provider/broker/executor imports,
no new dependencies (scipy/sklearn are deliberately avoided so the module loads
everywhere the base install runs). Every function is total: on insufficient or
malformed input it returns ``nan`` / an empty array / a zero size rather than
raising, so the advisory layer that consumes it can never blow up the caller.

Design note — why inverse-variance, not mean-variance:
    Classic mean-variance optimisation needs expected returns, which we do not
    reliably have; it is famously unstable to those estimates. The return-free
    baseline here is **naive risk parity** (inverse-variance weights) plus a
    volatility target and a correlation penalty — robust, cheap, reproducible.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def _to_1d_array(values) -> np.ndarray:
    """Coerce a Series/list/array of numbers to a 1-D float array (no NaN filter)."""
    if isinstance(values, pd.Series):
        arr = values.to_numpy(dtype=float)
    else:
        arr = np.asarray(values, dtype=float).ravel()
    return arr


def periodic_returns(prices) -> np.ndarray:
    """Simple period-over-period returns from a price series.

    Non-finite prices are dropped before differencing. Returns an empty array
    when fewer than two finite prices are available.
    """
    arr = _to_1d_array(prices)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return np.empty(0, dtype=float)
    prev = arr[:-1]
    curr = arr[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(prev != 0, (curr - prev) / prev, np.nan)
    return rets[np.isfinite(rets)]


def annualized_volatility(returns, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised volatility (sample std × √periods) of a return series.

    Returns ``nan`` when there are fewer than two finite returns (an honest
    "unknown" the sizing orchestrator guards on, rather than a fake 0.0 that
    would make a volatility target divide-by-zero).
    """
    arr = _to_1d_array(returns)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return float("nan")
    sd = float(np.std(arr, ddof=1))
    return sd * math.sqrt(max(periods_per_year, 1))


def inverse_variance_weights(cov) -> np.ndarray:
    """Naive risk-parity weights: proportional to 1/variance, summing to 1.

    Accepts a 1-D vector of variances or a 2-D covariance matrix (its diagonal
    is used). Assets with a non-finite or non-positive variance get zero weight;
    if none are usable the result is an equal-weight vector (a safe default).
    """
    mat = np.asarray(cov, dtype=float)
    if mat.ndim == 2:
        variances = np.diag(mat).astype(float)
    else:
        variances = mat.ravel().astype(float)

    n = variances.size
    if n == 0:
        return np.empty(0, dtype=float)

    inv = np.zeros(n, dtype=float)
    valid = np.isfinite(variances) & (variances > 0)
    inv[valid] = 1.0 / variances[valid]

    total = inv.sum()
    if total <= 0:
        return np.full(n, 1.0 / n, dtype=float)
    return inv / total


def volatility_target_weight(
    asset_volatility: float,
    target_volatility: float,
    max_weight: float = 1.0,
) -> float:
    """Fraction of the capital cap to allocate so position vol ≈ target.

    ``target / asset`` clamped to ``[0, max_weight]``: a name more volatile than
    the target is scaled down; the cap (default 1.0) means we never lever *up*
    past the base allocation just because a name is unusually quiet. Returns
    ``nan`` when either input is unusable, so the caller can skip the overlay.
    """
    if not (math.isfinite(asset_volatility) and math.isfinite(target_volatility)):
        return float("nan")
    if asset_volatility <= 0 or target_volatility < 0:
        return float("nan")
    return float(min(max(target_volatility / asset_volatility, 0.0), max_weight))


def average_correlation(candidate_returns, book_returns: Sequence) -> float:
    """Mean pairwise correlation of a candidate against each existing holding.

    Each holding's returns are tail-aligned with the candidate (the most recent
    overlapping window is used), so callers need only pass most-recent-last
    series of any lengths. Pairs with < 2 overlapping points or zero variance are
    skipped. Returns ``nan`` when nothing is computable (e.g. an empty book).
    """
    cand = _to_1d_array(candidate_returns)
    cand = cand[np.isfinite(cand)]
    if cand.size < 2 or not book_returns:
        return float("nan")

    corrs: list[float] = []
    for other in book_returns:
        arr = _to_1d_array(other)
        arr = arr[np.isfinite(arr)]
        k = min(cand.size, arr.size)
        if k < 2:
            continue
        a = cand[-k:]
        b = arr[-k:]
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if math.isfinite(c):
            corrs.append(c)

    if not corrs:
        return float("nan")
    return float(np.mean(corrs))


def correlation_scaled_shares(
    base_shares: float,
    avg_correlation: float,
    max_penalty: float = 0.5,
) -> int:
    """Shrink a base share count as candidate↔book correlation rises.

    ``factor = 1 - max_penalty · clamp(avg_correlation, 0, 1)`` — a name perfectly
    correlated with the book loses ``max_penalty`` of its size; a diversifying
    (≤ 0 correlation) or unknown (``nan``) name is left untouched. Never returns
    a negative count.
    """
    if base_shares <= 0:
        return 0
    if avg_correlation is None or not math.isfinite(avg_correlation):
        return int(base_shares)
    penalty = max(0.0, min(max_penalty, 1.0)) * max(0.0, min(avg_correlation, 1.0))
    return int(max(0.0, base_shares * (1.0 - penalty)))


def suggest_shares(
    *,
    price: float,
    equity: float,
    per_share_risk: float,
    risk_per_trade: float,
    max_position_pct: float,
    asset_volatility: float | None = None,
    target_volatility: float | None = None,
    avg_correlation: float | None = None,
    max_correlation_penalty: float = 0.5,
) -> dict:
    """Advisory share suggestion combining risk, volatility, and correlation.

    Layers, most-binding-wins:
      1. **Risk budget** — ``equity·risk_per_trade / per_share_risk`` (the 1%-rule
         base, same idea as ``trade_plan``).
      2. **Capital cap** — ``equity·max_position_pct / price``.
      3. **Volatility target** — scales the capital-cap allocation by
         ``target_vol / asset_vol`` (skipped when vols are unknown).
      4. **Correlation penalty** — shrinks the result toward diversification.

    Returns a JSON-friendly dict with ``suggested_shares`` and each component, so
    the recommendation is fully attributable. Never raises; unusable inputs
    yield ``suggested_shares == 0`` with a ``reason``.
    """
    components: dict = {
        "shares_by_risk": None,
        "shares_by_cap": None,
        "vol_target_weight": None,
        "shares_by_vol": None,
        "avg_correlation": (
            float(avg_correlation)
            if avg_correlation is not None and math.isfinite(avg_correlation)
            else None
        ),
    }

    if not (price > 0 and equity > 0 and per_share_risk > 0):
        return {
            "suggested_shares": 0,
            "reason": "non-positive price, equity, or per-share risk",
            **components,
        }

    shares_by_risk = (equity * risk_per_trade) / per_share_risk
    shares_by_cap = (equity * max_position_pct) / price
    components["shares_by_risk"] = float(shares_by_risk)
    components["shares_by_cap"] = float(shares_by_cap)

    base = min(shares_by_risk, shares_by_cap)

    # Volatility-target overlay (only when both vols are known and positive).
    if asset_volatility is not None and target_volatility is not None:
        vol_weight = volatility_target_weight(asset_volatility, target_volatility)
        if math.isfinite(vol_weight):
            shares_by_vol = vol_weight * shares_by_cap
            components["vol_target_weight"] = float(vol_weight)
            components["shares_by_vol"] = float(shares_by_vol)
            base = min(base, shares_by_vol)

    suggested = correlation_scaled_shares(base, avg_correlation, max_correlation_penalty)

    return {
        "suggested_shares": int(max(0, suggested)),
        "reason": "ok" if suggested > 0 else "sized below one share",
        **components,
    }
