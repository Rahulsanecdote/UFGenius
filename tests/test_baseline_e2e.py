"""End-to-end coverage for the composite → baseline → live-gate chain.

`tests/test_baseline.py` covers each piece against hand-built dictionaries. That
leaves one seam untested: whether a **real** `validate_strategy` result — the
dict `--mode validate --save-baseline` actually produces — carries the fields
`build_baseline` reads. A rename on either side would pass every unit test and
only surface at the end of a multi-hour production validation run, which is the
worst possible place to discover it.

These tests run the production path (composite replay → walk-forward → held-out
OOS → bootstrap → baseline → tolerance gate) with the network seam replaced by
deterministic synthetic bars. No network, no broker.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import src.utils.config as cfg
from src.backtest import engine
from src.backtest.baseline import (
    build_baseline,
    compare_paper_to_baseline,
    load_baseline,
    save_baseline,
)
from src.backtest.validation import validate_strategy

TICKERS = ["AAA", "BBB"]
START, END = "2019-06-01", "2020-08-01"


def _synth(ticker: str, n: int = 700) -> pd.DataFrame:
    """A trending series tuned to clear the composite BUY threshold.

    Deterministic per ticker: the composite needs a genuine uptrend with volume
    confirmation to label a bar BUY, so a flat random walk would yield zero
    entries and silently make these tests vacuous (the failure mode a fixture
    audit caught on this branch before).
    """
    rng = np.random.default_rng(11 + sum(map(ord, ticker)))
    steps = rng.normal(0.0013, 0.008, n)
    for i in range(0, n, 110):  # periodic pullbacks so momentum cycles
        steps[i : i + 3] -= 0.008
    close = np.maximum(60 * np.exp(np.cumsum(steps)), 1.0)
    intraday = np.abs(rng.normal(0.006, 0.003, n))
    rising = np.r_[True, np.diff(close) > 0]
    volume = np.where(
        rising,
        rng.integers(4_000_000, 9_000_000, n),
        rng.integers(1_000_000, 3_500_000, n),
    ).astype(float)
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.0015, n))
    return pd.DataFrame(
        {
            "Open": open_,
            "High": close * (1 + intraday),
            "Low": close * (1 - intraday),
            "Close": close,
            "Volume": volume,
        },
        index=pd.bdate_range("2018-01-01", periods=n),
    )


@pytest.fixture(scope="module", autouse=True)
def _composite_mode():
    """Hold composite mode for the whole module.

    `build_baseline` stamps provenance from the *live* config at build time, so
    the source has to stay set past the backtest and through baseline
    construction — exactly as `cmd_validate --save-baseline` does it in one
    process. Restoring it earlier silently produces a `proxy` baseline that the
    gate then refuses.

    Stride 5 keeps this affordable in the default suite: these tests are about
    the plumbing between stages, not the fidelity of the replay, and stride 1
    scores every bar for no extra coverage here.

    The other two pins isolate the fixture from ambient configuration that
    would otherwise decide whether it trades at all:

    - `universe_history_path` is a supported production setting, and
      `backtest_signal_system` auto-loads it when the caller passes nothing
      (`engine._entry_candidates_for_date` then skips any ticker that is not a
      member on the date). A real S&P membership file does not contain the
      synthetic tickers below, so the run would produce zero trades on a
      developer machine that has one configured while staying green in CI.
    - `composite_min_score` is a tunable strategy knob. Raising it in
      `config.yaml` would drop the fixture below the entry threshold and break
      this plumbing test for an unrelated reason.

    Both are pinned rather than left to the environment so a failure here means
    the chain broke, not that someone tuned the strategy.
    """
    original_source = cfg.BACKTEST_SIGNAL_SOURCE
    original_stride = cfg.BACKTEST_COMPOSITE_STRIDE
    original_history = cfg.BACKTEST_UNIVERSE_HISTORY_PATH
    original_min_score = cfg.BACKTEST_COMPOSITE_MIN_SCORE
    cfg.BACKTEST_SIGNAL_SOURCE = "composite"
    cfg.BACKTEST_COMPOSITE_STRIDE = 5
    cfg.BACKTEST_UNIVERSE_HISTORY_PATH = None
    cfg.BACKTEST_COMPOSITE_MIN_SCORE = 65.0
    try:
        yield
    finally:
        cfg.BACKTEST_SIGNAL_SOURCE = original_source
        cfg.BACKTEST_COMPOSITE_STRIDE = original_stride
        cfg.BACKTEST_UNIVERSE_HISTORY_PATH = original_history
        cfg.BACKTEST_COMPOSITE_MIN_SCORE = original_min_score


@pytest.fixture(scope="module")
def validation_result() -> dict:
    """One real composite validation run, shared across the tests below."""
    frames = {t: _synth(t) for t in TICKERS}
    original_fetch = engine.fetch_ohlcv
    engine.fetch_ohlcv = lambda ticker, **_k: frames.get(ticker.upper(), pd.DataFrame()).copy()
    try:
        return validate_strategy(
            TICKERS, START, END,
            initial_capital=10_000, n_windows=2, n_bootstrap=100,
            oos_fraction=0.30, seed=12345,
        )
    finally:
        engine.fetch_ohlcv = original_fetch


@pytest.fixture
def baseline(validation_result) -> dict:
    return build_baseline(
        validation_result, tickers=TICKERS, start=START, end=END,
        initial_capital=10_000, seed=12345,
    )


class TestRealResultFeedsTheBaseline:
    def test_composite_replay_actually_traded(self, validation_result):
        # Guards the fixture itself: zero OOS trades would make every assertion
        # below vacuous rather than failing loudly.
        assert validation_result["out_of_sample"]["total_trades"] > 0

    def test_baseline_reads_the_comparable_metrics_off_a_real_result(
        self, validation_result, baseline
    ):
        # The seam: these two keys are what the tolerance gate compares, and
        # they must survive the trip from validate_strategy's OOS block.
        # Asserted by value, not merely non-null: the baseline must carry the
        # HELD-OUT metrics, and a non-null check would pass just as happily on
        # in-sample or walk-forward numbers — which is the whole distinction
        # this module exists to pin down.
        metrics = baseline["metrics"]
        oos = validation_result["out_of_sample"]
        assert metrics["win_rate_pct"] == oos["win_rate_pct"]
        assert metrics["profit_factor"] == oos["profit_factor"]
        assert metrics["total_trades"] == oos["total_trades"]

    def test_provenance_records_the_signal_source(self, baseline):
        # A proxy baseline is refused by the gate, so the source must be
        # recorded from the run that produced it, not assumed.
        assert baseline["provenance"]["signal_source"] == "composite"

    def test_save_load_round_trip_preserves_the_gate_inputs(self, validation_result, tmp_path):
        path = str(tmp_path / "baseline.json")
        saved = save_baseline(
            validation_result, tickers=TICKERS, start=START, end=END,
            initial_capital=10_000, seed=12345, path=path,
        )
        reloaded = load_baseline(path)
        # Whole-record equality: the gate reads the file, not the in-memory
        # record, so anything the JSON round trip drops or coerces is a real
        # divergence — including fields these tests do not name individually.
        assert reloaded == saved


class TestGateAgainstARealBaseline:
    """The tolerance gate, driven by a baseline built from a real run."""

    @staticmethod
    def _validated(baseline: dict) -> dict:
        # This synthetic run's own verdict is not the subject here — isolate the
        # plumbing from whether the fixture happened to clear the edge floors.
        forced = json.loads(json.dumps(baseline))
        forced["validated"] = True
        return forced

    @staticmethod
    def _card(baseline: dict, factor: float) -> dict:
        m = baseline["metrics"]
        return {
            "n_trades": 40,
            "win_rate_pct": m["win_rate_pct"] * factor,
            "profit_factor": m["profit_factor"] * factor,
        }

    def test_matching_paper_passes(self, baseline):
        forced = self._validated(baseline)
        cmp_ = compare_paper_to_baseline(
            self._card(forced, 1.0), baseline=forced, tolerance_pct=20.0
        )
        assert cmp_["all_pass"] is True

    def test_underperforming_paper_blocks(self, baseline):
        forced = self._validated(baseline)
        cmp_ = compare_paper_to_baseline(
            self._card(forced, 0.4), baseline=forced, tolerance_pct=20.0
        )
        assert cmp_["all_pass"] is False
        assert "win_rate_pct" in cmp_["reason"]

    def test_outperforming_paper_never_blocks(self, baseline):
        # One-sided by design: paper fills carry no real slippage, so
        # paper-better-than-backtest is the expected direction of error.
        forced = self._validated(baseline)
        cmp_ = compare_paper_to_baseline(
            self._card(forced, 1.6), baseline=forced, tolerance_pct=20.0
        )
        assert cmp_["all_pass"] is True

    def test_a_proxy_baseline_is_still_refused(self, baseline):
        forced = self._validated(baseline)
        forced["provenance"]["signal_source"] = "proxy"
        cmp_ = compare_paper_to_baseline(
            self._card(baseline, 1.0), baseline=forced, tolerance_pct=20.0
        )
        assert cmp_["all_pass"] is False
