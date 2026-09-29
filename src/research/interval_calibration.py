"""Is a forecast interval honest, and is it worth more than a naive one?

Written for the Kronos evaluation (`docs/KRONOS_EVALUATION.md`) but forecaster-
agnostic: anything that can answer "given bars up to today, where will the close
be in `h` bars, at the `level` confidence band" can be measured here.

**Coverage is necessary and nowhere near sufficient.** A 90% band should contain
the realised close 90% of the time, and a band that contains it 60% of the time
is decoration. But coverage alone cannot rank forecasters, because it is trivial
to be perfectly calibrated and useless: widen the band until it always contains
the answer. The discriminating question is *sharpness subject to calibration* —
among forecasters with correct coverage, the narrower band knows more.

So every run reports three things together, and reading any one alone is a
mistake:

* **coverage** — did the band do what it claimed?
* **width** — how much did it claim, as a percent of price?
* **interval score** (Winkler) — the proper scoring rule that trades the two
  off, so it cannot be gamed in either direction. Lower is better.

  ``IS = (u - l) + (2/a)(l - y) if y < l, + (2/a)(y - u) if y > u``

  for a central ``1 - a`` interval. Widening costs width on every window;
  narrowing costs a miss penalty scaled by ``2/a``, which at the 90% level is
  20x the shortfall. There is no free direction.

**And there must be a baseline.** The Kronos repository reports directional
accuracy, mean absolute error, a "<5% error" rate, correlation and a Sharpe —
against nothing. Two of those are actively misleading on price series:
correlation of predicted and actual *levels* is ~1 for any method including
"tomorrow equals today", and on a one-day horizon a <5% error is met by the
naive forecast nearly always. A forecaster is only interesting relative to what
you would have had for free, so `naive_gaussian` and `naive_empirical` ship here
and every run measures them on the identical windows.

No look-ahead: for each origin the forecaster sees bars up to and including the
origin and nothing after, the baselines estimate their volatility from that same
slice, and the comparison is against the close `h` bars later.

Origins are spaced `stride` bars apart, defaulting to `horizon`, so the
evaluation windows do **not** overlap. Overlapping windows share most of their
price path, which makes successive hits correlated and any confidence interval
on the coverage estimate far too narrow. Non-overlapping costs sample size and
buys an interval that means what it says.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)

__all__ = [
    "Band",
    "Forecaster",
    "naive_gaussian",
    "naive_empirical",
    "evaluate_calibration",
    "compare_forecasters",
    "interval_score",
    "wilson_interval",
]


@dataclass(frozen=True)
class Band:
    """A forecast for the close ``horizon`` bars after the origin.

    ``lower``/``upper`` are the central-interval endpoints at the requested
    level; ``median`` is the point forecast and may be None — a forecaster that
    only produces an interval is still measurable here, which is the point.
    """

    lower: float
    upper: float
    median: Optional[float] = None

    def valid(self) -> bool:
        try:
            return (
                math.isfinite(self.lower)
                and math.isfinite(self.upper)
                and self.upper >= self.lower
            )
        except Exception:
            return False


# (bars up to and including the origin, horizon, level) -> Band or None.
# Returning None means "cannot forecast this window"; those windows are counted
# as skipped rather than dropped silently, because a forecaster that quietly
# declines the hard windows would post excellent coverage on the easy ones.
Forecaster = Callable[[pd.DataFrame, int, float], Optional[Band]]


def _z(level: float) -> float:
    """Two-sided normal critical value for a central `level` interval.

    `statistics.NormalDist` rather than scipy: scipy is not a dependency here
    and this is the only distribution function needed.
    """
    from statistics import NormalDist

    alpha = 1.0 - float(level)
    return float(NormalDist().inv_cdf(1.0 - alpha / 2.0))


def _log_returns(closes: np.ndarray) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    c = c[np.isfinite(c) & (c > 0)]
    if c.size < 2:
        return np.array([])
    return np.diff(np.log(c))


def naive_gaussian(
    bars: pd.DataFrame, horizon: int, level: float, *, vol_window: int = 120
) -> Optional[Band]:
    """Random walk with Gaussian steps: the cheapest defensible band.

    ``close * exp(+/- z * sigma * sqrt(h))`` with sigma the trailing one-step
    log-return standard deviation. Assumes normality, which returns violate in
    the tails — so this baseline is *expected* to under-cover, and by how much
    is itself informative about the sample.
    """
    try:
        closes = bars["Close"].to_numpy(dtype=float)
        rets = _log_returns(closes[-(vol_window + 1):])
        if rets.size < 20:
            return None
        sigma = float(np.std(rets, ddof=1))
        last = float(closes[-1])
        if not (math.isfinite(sigma) and sigma > 0 and last > 0):
            return None
        spread = _z(level) * sigma * math.sqrt(max(1, int(horizon)))
        return Band(last * math.exp(-spread), last * math.exp(spread), last)
    except Exception:
        return None


def naive_empirical(
    bars: pd.DataFrame, horizon: int, level: float, *, sample_window: int = 500
) -> Optional[Band]:
    """Empirical quantiles of trailing `horizon`-step log returns.

    The harder baseline, and the one a forecast model actually has to beat: it
    makes no distributional assumption, so it inherits the real tails. If
    returns are anywhere near stationary this is calibrated by construction —
    which is exactly why coverage alone cannot crown a winner, and why the
    interval score is reported beside it.
    """
    try:
        closes = bars["Close"].to_numpy(dtype=float)
        closes = closes[np.isfinite(closes) & (closes > 0)]
        h = max(1, int(horizon))
        window = closes[-(sample_window + h):]
        if window.size < h + 30:
            return None
        # Overlapping h-step returns: fewer independent observations than it
        # looks, but the quantile estimate is still consistent and this is a
        # baseline, not the thing under test.
        steps = np.log(window[h:] / window[:-h])
        steps = steps[np.isfinite(steps)]
        if steps.size < 30:
            return None
        alpha = 1.0 - float(level)
        lo, hi = np.quantile(steps, [alpha / 2.0, 1.0 - alpha / 2.0])
        last = float(closes[-1])
        return Band(last * math.exp(float(lo)), last * math.exp(float(hi)), last)
    except Exception:
        return None


def interval_score(lower: float, upper: float, actual: float, level: float) -> float:
    """Winkler interval score. Lower is better; never negative."""
    alpha = max(1e-9, 1.0 - float(level))
    score = float(upper) - float(lower)
    if actual < lower:
        score += (2.0 / alpha) * (lower - actual)
    elif actual > upper:
        score += (2.0 / alpha) * (actual - upper)
    return score


def wilson_interval(hits: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Wilson rather than the normal approximation because coverage estimates sit
    near 0.9, where the naive interval runs past 1.0 and stops being readable.
    """
    if n <= 0:
        return (float("nan"), float("nan"))
    z = _z(level)
    p = hits / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def evaluate_calibration(
    bars: pd.DataFrame,
    forecaster: Forecaster,
    *,
    horizon: int,
    level: float = 0.90,
    warmup: int = 400,
    stride: Optional[int] = None,
    max_origins: Optional[int] = None,
    label: str = "forecaster",
) -> dict:
    """Walk `bars` forward and score one forecaster's intervals.

    `stride` defaults to `horizon` so evaluation windows do not overlap; the
    returned `windows_overlap` flag says which it was, because an overlapping
    run's confidence interval is not trustworthy and the result has to carry
    that rather than rely on the caller remembering.
    """
    h = max(1, int(horizon))
    step = h if stride is None else max(1, int(stride))
    out: dict = {
        "label": label, "horizon": h, "level": float(level),
        "stride": step, "windows_overlap": step < h, "warmup": int(warmup),
    }
    if bars is None or len(bars) < warmup + h + 1 or "Close" not in bars:
        out["error"] = (
            f"need at least warmup+horizon+1 = {warmup + h + 1} bars, "
            f"have {0 if bars is None else len(bars)}"
        )
        return out

    closes = bars["Close"].to_numpy(dtype=float)
    hits = 0
    skipped = 0
    widths: list[float] = []
    scores: list[float] = []
    med_abs_err: list[float] = []
    origins: list[int] = []

    t = int(warmup)
    while t + h < len(bars):
        if max_origins is not None and len(origins) >= max_origins:
            break
        actual = float(closes[t + h])
        band = None
        try:
            band = forecaster(bars.iloc[: t + 1], h, float(level))
        except Exception as exc:
            log.debug(f"{label}: forecaster raised at origin {t} ({type(exc).__name__})")
        if band is None or not band.valid() or not math.isfinite(actual) or actual <= 0:
            skipped += 1
        else:
            origins.append(t)
            if band.lower <= actual <= band.upper:
                hits += 1
            # Normalised by the realised close so names of different price
            # levels can be pooled at all.
            widths.append((band.upper - band.lower) / actual * 100.0)
            scores.append(interval_score(band.lower, band.upper, actual, level)
                          / actual * 100.0)
            if band.median is not None and math.isfinite(band.median):
                med_abs_err.append(abs(band.median - actual) / actual * 100.0)
        t += step

    n = len(origins)
    out.update({
        "n_windows": n,
        "skipped": skipped,
        "hits": hits,
        "coverage": (hits / n) if n else None,
        "coverage_ci95": list(wilson_interval(hits, n)) if n else None,
        "mean_width_pct": float(np.mean(widths)) if widths else None,
        "median_width_pct": float(np.median(widths)) if widths else None,
        "mean_interval_score_pct": float(np.mean(scores)) if scores else None,
        "median_abs_point_error_pct": (
            float(np.median(med_abs_err)) if med_abs_err else None),
    })
    if n and n < 30:
        out["warning"] = (
            f"n={n} is too small to read coverage from: the 95% interval spans "
            f"{out['coverage_ci95'][0]:.2f}-{out['coverage_ci95'][1]:.2f}"
        )
    return out


def compare_forecasters(
    bars: pd.DataFrame,
    forecasters: dict[str, Forecaster],
    *,
    horizon: int,
    level: float = 0.90,
    warmup: int = 400,
    stride: Optional[int] = None,
    max_origins: Optional[int] = None,
) -> dict:
    """Score several forecasters on the identical windows and rank them.

    Identical windows matter more than it sounds: run two forecasters over
    different origin sets and the comparison silently becomes a comparison of
    market conditions. `max_origins` and the shared `stride`/`warmup` pin them
    to the same grid, and `common_n` reports whether they actually answered on
    the same count.
    """
    results = {
        name: evaluate_calibration(
            bars, fc, horizon=horizon, level=level, warmup=warmup,
            stride=stride, max_origins=max_origins, label=name,
        )
        for name, fc in forecasters.items()
    }
    scored = {k: v for k, v in results.items()
              if v.get("mean_interval_score_pct") is not None}
    ranking = sorted(scored, key=lambda k: scored[k]["mean_interval_score_pct"])
    # Deliberately NOT filtering zero/None: a forecaster that answered no
    # windows is the mismatch this field exists to reveal, so dropping it here
    # would report agreement between one real run and one that never happened.
    ns = {k: v.get("n_windows") for k, v in results.items()}
    return {
        "level": float(level), "horizon": int(horizon),
        "results": results,
        "ranked_by_interval_score": ranking,
        "n_windows_per_forecaster": ns,
        "common_n": len(set(ns.values())) == 1 and all(
            isinstance(n, int) and n > 0 for n in ns.values()),
        "how_to_read": (
            "Coverage near the level means the band is honest. Among honest "
            "bands the narrower one knows more, and mean_interval_score_pct is "
            "the proper trade-off between the two — lower wins. A forecaster "
            "that cannot beat naive_empirical on interval score at correct "
            "coverage has shown nothing."
        ),
    }
