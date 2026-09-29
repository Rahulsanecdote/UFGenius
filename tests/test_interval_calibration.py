"""The calibration harness has to be proven before it can judge anything.

Every test here runs on synthetic data whose answer is known in advance. A
measurement tool that has only ever been pointed at real data has not been
tested — it has been used, and a bug in it looks exactly like a finding.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.research.interval_calibration import (
    Band,
    compare_forecasters,
    evaluate_calibration,
    interval_score,
    naive_empirical,
    naive_gaussian,
    wilson_interval,
)

SIGMA = 0.02          # 2% daily log-return vol
N_BARS = 6000


def _gbm(n: int = N_BARS, sigma: float = SIGMA, seed: int = 7,
         mu: float = 0.0) -> pd.DataFrame:
    """A Gaussian random walk — the one process whose correct band is known."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(mu, sigma, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    idx = pd.date_range("2000-01-03", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": close, "High": close * 1.001, "Low": close * 0.999,
         "Close": close, "Volume": np.full(n, 1_000_000)},
        index=idx,
    )


def _oracle(sigma: float = SIGMA):
    """The correctly-specified band for the process above. Must hit nominal."""
    def fc(bars, horizon, level):
        from statistics import NormalDist
        z = NormalDist().inv_cdf(1.0 - (1.0 - level) / 2.0)
        last = float(bars["Close"].iloc[-1])
        spread = z * sigma * math.sqrt(horizon)
        return Band(last * math.exp(-spread), last * math.exp(spread), last)
    return fc


class TestTheHarnessMeasuresWhatItClaims:
    def test_a_correct_band_recovers_the_nominal_level(self):
        """If this fails, nothing else in the file means anything."""
        r = evaluate_calibration(_gbm(), _oracle(), horizon=5, level=0.90,
                                 warmup=400, label="oracle")
        assert r["n_windows"] > 500
        lo, hi = r["coverage_ci95"]
        assert lo <= 0.90 <= hi, f"coverage {r['coverage']:.3f} CI {lo:.3f}-{hi:.3f}"

    @pytest.mark.parametrize("level", [0.50, 0.80, 0.95])
    def test_it_recovers_other_levels_too(self, level):
        r = evaluate_calibration(_gbm(), _oracle(), horizon=5, level=level,
                                 warmup=400)
        lo, hi = r["coverage_ci95"]
        assert lo <= level <= hi, f"level {level}: {lo:.3f}-{hi:.3f}"

    def test_a_too_narrow_band_is_caught(self):
        """The failure mode that matters: a model whose band flatters itself."""
        r = evaluate_calibration(_gbm(), _oracle(sigma=SIGMA / 3), horizon=5,
                                 level=0.90, warmup=400)
        assert r["coverage"] < 0.70
        assert r["coverage_ci95"][1] < 0.90   # nominal excluded, not merely low

    def test_a_too_wide_band_is_also_visible(self):
        """Over-covering is not success — it is a band that claims nothing."""
        r = evaluate_calibration(_gbm(), _oracle(sigma=SIGMA * 3), horizon=5,
                                 level=0.90, warmup=400)
        assert r["coverage"] > 0.98
        narrow = evaluate_calibration(_gbm(), _oracle(), horizon=5, level=0.90,
                                      warmup=400)
        # Correct coverage, far narrower band => strictly better interval score.
        assert r["mean_width_pct"] > narrow["mean_width_pct"] * 2
        assert r["mean_interval_score_pct"] > narrow["mean_interval_score_pct"]


class TestTheIntervalScoreCannotBeGamed:
    """Coverage alone can be gamed in both directions. The score cannot."""

    def test_widening_to_guarantee_coverage_loses(self):
        wide = evaluate_calibration(_gbm(), _oracle(sigma=SIGMA * 10), horizon=5,
                                    level=0.90, warmup=400)
        right = evaluate_calibration(_gbm(), _oracle(), horizon=5, level=0.90,
                                     warmup=400)
        assert wide["coverage"] == pytest.approx(1.0, abs=0.01)
        assert wide["mean_interval_score_pct"] > right["mean_interval_score_pct"]

    def test_narrowing_to_look_sharp_also_loses(self):
        thin = evaluate_calibration(_gbm(), _oracle(sigma=SIGMA / 10), horizon=5,
                                    level=0.90, warmup=400)
        right = evaluate_calibration(_gbm(), _oracle(), horizon=5, level=0.90,
                                     warmup=400)
        assert thin["mean_width_pct"] < right["mean_width_pct"]
        assert thin["mean_interval_score_pct"] > right["mean_interval_score_pct"]

    def test_the_formula_matches_its_definition(self):
        # Inside: the score is just the width.
        assert interval_score(90.0, 110.0, 100.0, 0.90) == pytest.approx(20.0)
        # Below by 5 at the 90% level: width + (2/0.10)*5 = 20 + 100.
        assert interval_score(90.0, 110.0, 85.0, 0.90) == pytest.approx(120.0)
        # Above by 5: symmetric.
        assert interval_score(90.0, 110.0, 115.0, 0.90) == pytest.approx(120.0)

    def test_a_miss_is_penalised_harder_at_a_higher_level(self):
        """2/alpha scaling: a 99% band that misses should hurt far more."""
        at90 = interval_score(90.0, 110.0, 85.0, 0.90)
        at99 = interval_score(90.0, 110.0, 85.0, 0.99)
        assert at99 > at90 * 5


class TestTheBaselines:
    def test_empirical_is_calibrated_on_a_stationary_process(self):
        """Which is the point of having it: this is what a model must beat."""
        r = evaluate_calibration(_gbm(), naive_empirical, horizon=5, level=0.90,
                                 warmup=600)
        assert r["n_windows"] > 400
        assert 0.85 <= r["coverage"] <= 0.95, r["coverage"]

    def test_gaussian_is_roughly_calibrated_on_a_gaussian_process(self):
        r = evaluate_calibration(_gbm(), naive_gaussian, horizon=5, level=0.90,
                                 warmup=600)
        assert 0.85 <= r["coverage"] <= 0.95, r["coverage"]

    def test_gaussian_under_covers_on_fat_tails(self):
        """The assumption this baseline makes, and where it breaks — stated as a
        test so the reported number is read with it in mind."""
        rng = np.random.default_rng(11)
        steps = rng.standard_t(df=3, size=N_BARS) * 0.012   # fat-tailed
        close = 100.0 * np.exp(np.cumsum(steps))
        bars = pd.DataFrame(
            {"Open": close, "High": close, "Low": close, "Close": close,
             "Volume": np.full(N_BARS, 1e6)},
            index=pd.date_range("2000-01-03", periods=N_BARS, freq="B"))
        r = evaluate_calibration(bars, naive_gaussian, horizon=5, level=0.95,
                                 warmup=600)
        assert r["coverage"] < 0.95

    def test_baselines_decline_rather_than_guess_without_history(self):
        # naive_gaussian needs 20 returns and naive_empirical h+30 closes;
        # 15 bars clears neither, and declining beats a vol estimate from noise.
        short = _gbm(n=15)
        assert naive_gaussian(short, 5, 0.90) is None
        assert naive_empirical(short, 5, 0.90) is None


class TestNoLookAhead:
    def test_the_forecaster_never_sees_past_the_origin(self):
        """The bug that makes every forecast look brilliant."""
        bars = _gbm(n=1200)
        seen: list[int] = []

        def spy(window, horizon, level):
            seen.append(len(window))
            # The window's last close must equal the frame's close at that index.
            assert window["Close"].iloc[-1] == bars["Close"].iloc[len(window) - 1]
            return _oracle()(window, horizon, level)

        evaluate_calibration(bars, spy, horizon=10, level=0.90, warmup=400)
        assert seen and max(seen) <= len(bars) - 10
        # Strictly increasing by the stride: no window is revisited or reordered.
        assert all(b - a == 10 for a, b in zip(seen, seen[1:]))


class TestHonestyOfTheReport:
    def test_non_overlapping_is_the_default_and_is_disclosed(self):
        r = evaluate_calibration(_gbm(), _oracle(), horizon=7, level=0.90,
                                 warmup=400)
        assert r["stride"] == 7
        assert r["windows_overlap"] is False

    def test_overlap_is_flagged_when_the_caller_asks_for_it(self):
        r = evaluate_calibration(_gbm(), _oracle(), horizon=7, level=0.90,
                                 warmup=400, stride=1)
        assert r["windows_overlap"] is True

    def test_declined_windows_are_counted_not_dropped(self):
        """A forecaster that skips the hard windows would otherwise post
        excellent coverage on the easy ones with nothing to show the gap."""
        calls = {"n": 0}

        def picky(window, horizon, level):
            calls["n"] += 1
            if calls["n"] % 2:
                return None
            return _oracle()(window, horizon, level)

        r = evaluate_calibration(_gbm(), picky, horizon=5, level=0.90, warmup=400)
        assert r["skipped"] > 0
        assert r["n_windows"] + r["skipped"] == calls["n"]

    def test_a_raising_forecaster_is_a_skip_not_a_crash(self):
        def broken(window, horizon, level):
            raise RuntimeError("boom")

        r = evaluate_calibration(_gbm(), broken, horizon=5, level=0.90, warmup=400)
        assert r["n_windows"] == 0 and r["skipped"] > 0
        assert r["coverage"] is None

    def test_a_small_sample_says_so(self):
        r = evaluate_calibration(_gbm(n=900), _oracle(), horizon=20, level=0.90,
                                 warmup=400, max_origins=12)
        assert r["n_windows"] == 12
        assert "warning" in r and "too small" in r["warning"]

    def test_too_little_data_is_an_error_not_a_zero(self):
        r = evaluate_calibration(_gbm(n=100), _oracle(), horizon=5, level=0.90,
                                 warmup=400)
        assert "error" in r and r.get("coverage") is None

    def test_wilson_interval_brackets_the_estimate(self):
        lo, hi = wilson_interval(90, 100)
        assert lo < 0.90 < hi
        # More data, tighter interval — the whole reason n is reported.
        lo2, hi2 = wilson_interval(900, 1000)
        assert (hi2 - lo2) < (hi - lo)

    def test_no_windows_yields_nan_bounds_rather_than_a_raise(self):
        assert all(math.isnan(x) for x in wilson_interval(0, 0))


class TestComparison:
    def test_it_ranks_by_interval_score_and_pins_the_windows(self):
        bars = _gbm()
        out = compare_forecasters(
            bars,
            {"oracle": _oracle(), "too_wide": _oracle(sigma=SIGMA * 5),
             "naive_empirical": naive_empirical},
            horizon=5, level=0.90, warmup=600,
        )
        assert out["ranked_by_interval_score"][0] == "oracle"
        assert out["ranked_by_interval_score"][-1] == "too_wide"
        assert out["common_n"] is True

    def test_a_forecaster_that_answers_nothing_is_excluded_from_the_ranking(self):
        out = compare_forecasters(
            _gbm(),
            {"oracle": _oracle(), "mute": lambda *a: None},
            horizon=5, level=0.90, warmup=600,
        )
        assert out["ranked_by_interval_score"] == ["oracle"]
        assert out["results"]["mute"]["n_windows"] == 0
        assert out["common_n"] is False   # and the mismatch is visible


class TestFirewall:
    def test_the_module_cannot_reach_the_money_path(self):
        import pathlib
        src = pathlib.Path("src/research/interval_calibration.py").read_text()
        for forbidden in ("src.alpaca", "executor", "place_order",
                          "execute_trade_plan", "TradingClient"):
            assert forbidden not in src, forbidden
