"""Tests for pre-expansion (spike precursor) detection (src/signals/precursor.py).

Hermetic: frames are built by hand, thresholds come from the real config.

The detector's job is a volatility CONTRACTION followed by an expansion out of
it. The tests that matter most are the ones pinning what it must NOT do: fire on
a coil alone (which resolves in both directions), fire on a dead tape, or fire on
a break that nothing is behind.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

import src.utils.config as cfg
from src.signals import precursor as pc

_START = datetime(2026, 9, 25, 14, 0)     # 10:00 ET, mid-session


def _frame(bars):
    """bars: list of (high, low, close, volume); 5-minute spacing."""
    idx = pd.DatetimeIndex([_START + timedelta(minutes=5 * i) for i in range(len(bars))])
    return pd.DataFrame(
        {"Open": [b[2] for b in bars], "High": [b[0] for b in bars],
         "Low": [b[1] for b in bars], "Close": [b[2] for b in bars],
         "Volume": [b[3] for b in bars]},
        index=idx,
    )


def _baseline(n=22, lo=9.0, span=0.60, vol=100_000):
    """Wide-range, healthy-volume bars: the reference the coil is measured against."""
    return [(lo + span, lo, lo + span * 0.5, vol) for _ in range(n)]


def _coil(n=6, level=9.5, span=0.06, vol=40_000):
    """Tight-range, dried-up bars near the top of the baseline."""
    return [(level + span, level, level + span * 0.5, vol) for _ in range(n)]


def _expansion(level=9.5, span=0.06, mult_vol=4.0, coil_vol=40_000):
    """The trigger bar: clears the coil high with range and volume behind it."""
    return (level + span * 6, level + span * 0.5, level + span * 5,
            int(coil_vol * mult_vol))


def _full():
    return _frame(_baseline() + _coil() + [_expansion()])


# ── measurement ──────────────────────────────────────────────────────────────

class TestDetect:
    def test_compression_is_measured_against_a_non_overlapping_baseline(self):
        m = pc.detect_precursor(_full())
        assert m is not None
        # Coil span 0.06 against a 0.60 baseline — decisively compressed.
        assert m["compression"] < 0.5
        assert m["baseline_bars"] == cfg.PRECURSOR_BASELINE_BARS
        assert m["coil_bars"] == cfg.PRECURSOR_COIL_BARS

    def test_volume_dryup_is_the_coil_against_the_baseline(self):
        m = pc.detect_precursor(_full())
        assert m["volume_dryup"] < 1.0        # 40k coil vs 100k baseline

    def test_coil_high_excludes_the_newest_bar(self):
        """Otherwise the trigger bar is compared against itself and every new
        high 'breaks the coil'."""
        m = pc.detect_precursor(_full())
        assert m["coil_high"] == pytest.approx(9.56, abs=0.01)   # not the expansion bar's high
        assert m["last_price"] > m["coil_high"]

    def test_too_few_bars_is_unmeasurable_not_false(self):
        assert pc.detect_precursor(_frame(_baseline(n=6))) is None

    def test_a_flat_baseline_does_not_divide_by_zero(self):
        flat = [(9.0, 9.0, 9.0, 1000) for _ in range(40)]
        assert pc.detect_precursor(_frame(flat)) is None


# ── grading ──────────────────────────────────────────────────────────────────

class TestEvaluate:
    def test_coil_plus_expansion_is_an_entry(self):
        d = pc.evaluate_precursor(_full())
        assert d["enter"] is True
        assert d["signal"] in {"BUY", "STRONG_BUY"}
        assert any("compressed" in r for r in d["reasons"])
        assert any("coil high" in r for r in d["reasons"])

    def test_a_coil_alone_is_a_watch_not_an_entry(self):
        """The most important negative. Compression resolves in BOTH directions,
        so firing on it would be a coin flip dressed as a signal."""
        d = pc.evaluate_precursor(_frame(_baseline() + _coil(n=6)))
        assert d["enter"] is False
        assert d["signal"] == "HOLD"
        assert any("not yet expanding" in r for r in d["reasons"])

    def test_a_break_with_no_volume_behind_it_does_not_enter(self):
        quiet_break = (9.86, 9.53, 9.80, 8_000)      # range expands, volume does not
        d = pc.evaluate_precursor(_frame(_baseline() + _coil() + [quiet_break]))
        assert d["enter"] is False

    def test_no_contraction_means_no_setup_however_big_the_bar(self):
        """A big bar out of an already-wide tape is not an expansion out of a
        coil — there is nothing to expand out of."""
        big = (11.0, 9.0, 10.9, 500_000)
        d = pc.evaluate_precursor(_frame(_baseline(n=30) + [big]))
        assert d["enter"] is False
        assert any("No volatility contraction" in r for r in d["reasons"])

    def test_dryup_separates_a_coil_from_a_dead_tape(self):
        """Volume falling DURING the compression is what makes it a coil; the
        same shape on unchanged volume grades lower."""
        dry = pc.evaluate_precursor(_full())
        wet = pc.evaluate_precursor(
            _frame(_baseline() + _coil(vol=100_000) + [_expansion(coil_vol=100_000)]))
        assert dry["signal"] == "STRONG_BUY"
        assert wet["signal"] == "BUY"
        assert dry["score"] > wet["score"]

    def test_a_coil_low_in_the_days_range_is_refused(self):
        """Coiling at the LOW of the day is a different animal from coiling at
        the high, and this detector only looks for upside resolutions."""
        high_open = [(12.0, 11.4, 11.6, 100_000)]     # day high set early, far above
        d = pc.evaluate_precursor(_frame(high_open + _baseline() + _coil() + [_expansion()]))
        assert d["precursor"]["range_position"] < cfg.PRECURSOR_MIN_RANGE_POSITION
        assert d["enter"] is False

    def test_stop_hint_is_the_coil_low(self):
        d = pc.evaluate_precursor(_full())
        p = d["precursor"]
        assert p["stop_hint"] == p["coil_low"]
        assert p["stop_hint"] < d["current_price"]

    def test_insufficient_data_holds_rather_than_raising(self):
        d = pc.evaluate_precursor(_frame(_baseline(n=2)))
        assert d["enter"] is False and d["signal"] == "HOLD"
        assert d["precursor"] == {}

    def test_disabling_the_vwap_requirement_is_the_only_way_below_it(self):
        below = _frame(_baseline(lo=12.0, span=0.60) + _coil(level=9.5) + [_expansion()])
        assert pc.evaluate_precursor(below)["enter"] is False
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "PRECURSOR_REQUIRE_ABOVE_VWAP", False)
            # Still needs the range-position rule, so this only proves the VWAP
            # gate is what blocked it — not that the setup is otherwise good.
            d = pc.evaluate_precursor(below)
        assert d["precursor"]["above_vwap"] is False


# ── the superset used by a producer ──────────────────────────────────────────

class TestCoilPresent:
    def test_true_for_a_coil_without_expansion(self):
        assert pc.coil_present(_frame(_baseline() + _coil(n=6))) is True

    def test_false_for_a_wide_tape(self):
        assert pc.coil_present(_frame(_baseline(n=40))) is False

    def test_never_raises_on_junk(self):
        assert pc.coil_present(pd.DataFrame()) is False
        assert pc.coil_present(None) is False


# ── the backtest plug-in point ───────────────────────────────────────────────

def test_registered_as_a_backtestable_entry():
    """It must be measurable out-of-sample before it is allowed to alert."""
    from src.backtest.intraday_engine import _STRATEGIES

    assert "precursor" in _STRATEGIES
    assert _STRATEGIES["precursor"](_full()) is not None
    assert _STRATEGIES["precursor"](_frame(_baseline() + _coil(n=6))) is None


def test_default_off():
    assert cfg.PRECURSOR_ENABLED is False


def test_warmup_is_stated_and_enforced():
    """The constraint that decides whether this can see a morning move at all.

    28 bars is 28 minutes at 1m and 140 at 5m. Replaying MSGY (2026-09-25,
    $2.13 -> $5.95 between 09:30 and 11:07) at 5m yields nothing whatsoever,
    because the detector has no history to have an opinion with until the move
    is over. The number is exported so callers can see it rather than discover
    it as silence.
    """
    assert pc.warmup_bars() == cfg.PRECURSOR_BASELINE_BARS + cfg.PRECURSOR_COIL_BARS + 2
    one_short = _frame(_baseline(n=pc.warmup_bars() - 1))
    assert pc.detect_precursor(one_short) is None
    assert pc.detect_precursor(_frame(_baseline(n=pc.warmup_bars()))) is not None


# ── the cost floor ───────────────────────────────────────────────────────────
# Backtesting WITHOUT this returned profit factor 0.06 and an average loss of
# -2.83R on 50 S&P names — not a verdict on the signal but on the geometry. A
# measured trade risked $0.625/share on a $339 stock (0.184% of price) against
# $1.356 of modelled round-trip cost (0.400%): friction was 2.17x the entire
# risk unit, so a perfect entry stopping out exactly at its stop still lost ~2R.


class TestCostFloor:
    def _priced(self, price):
        """The same coil geometry at an arbitrary price level.

        Scaling the whole structure keeps compression, dry-up and expansion
        identical while changing only the stop distance AS A FRACTION OF PRICE
        — which is exactly what the floor measures.
        """
        k = price / 9.5
        return _frame(
            [(b[0] * k, b[1] * k, b[2] * k, b[3]) for b in _baseline()]
            + [(b[0] * k, b[1] * k, b[2] * k, b[3]) for b in _coil()]
            + [tuple([v * k for v in _expansion()[:3]] + [_expansion()[3]])]
        )

    def test_measured_and_exposed(self):
        m = pc.detect_precursor(self._priced(9.5))
        assert m["risk_pct"] is not None
        assert m["round_trip_cost_pct"] == pytest.approx(0.4, abs=0.01)
        assert m["risk_cost_multiple"] == pytest.approx(
            m["risk_pct"] / m["round_trip_cost_pct"], rel=1e-3)

    def test_the_same_setup_passes_cheap_and_fails_expensive(self):
        """The ONLY difference is price level. The coil is a fixed fraction of
        price here, so this isolates nothing but risk-vs-friction... which is
        the point: the floor must not care about the pattern, only the economics."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "BACKTEST_COMMISSION_PCT", 0.0)
            mp.setattr(cfg, "BACKTEST_SLIPPAGE_PCT", 0.0001)   # 1bp/side, liquid
            cheap = pc.evaluate_precursor(self._priced(9.5))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "BACKTEST_COMMISSION_PCT", 0.01)   # 1%/side, brutal
            mp.setattr(cfg, "BACKTEST_SLIPPAGE_PCT", 0.01)
            dear = pc.evaluate_precursor(self._priced(9.5))
        assert cheap["enter"] is True
        assert dear["enter"] is False
        assert any("too tight to pay for itself" in r for r in dear["reasons"])

    def test_the_refusal_states_both_numbers(self):
        """An operator must be able to see WHY without reading the source."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "BACKTEST_COMMISSION_PCT", 0.01)
            mp.setattr(cfg, "BACKTEST_SLIPPAGE_PCT", 0.01)
            d = pc.evaluate_precursor(self._priced(9.5))
        reason = next(r for r in d["reasons"] if "too tight" in r)
        assert "% of price" in reason and "round-trip cost" in reason
        assert "need 2.0x" in reason

    def test_zero_disables_it(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "BACKTEST_COMMISSION_PCT", 0.01)
            mp.setattr(cfg, "BACKTEST_SLIPPAGE_PCT", 0.01)
            mp.setattr(cfg, "PRECURSOR_MIN_RISK_COST_MULTIPLE", 0.0)
            assert pc.evaluate_precursor(self._priced(9.5))["enter"] is True

    def test_it_gates_the_backtest_entry_too(self):
        """The guard has to live in the evaluator, not the harness — otherwise
        the backtest and the live path would disagree about what a trade is."""
        from src.backtest.intraday_engine import _STRATEGIES

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cfg, "BACKTEST_COMMISSION_PCT", 0.01)
            mp.setattr(cfg, "BACKTEST_SLIPPAGE_PCT", 0.01)
            assert _STRATEGIES["precursor"](self._priced(9.5)) is None
