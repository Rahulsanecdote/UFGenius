"""The Kronos adapter, tested with a fake predictor — no torch, no weights.

What matters here is not that Kronos works; it is that the adapter asks it the
right question. Three of these pin decisions that, if wrong, would quietly bias
the calibration result in the model's favour: no nucleus truncation, enough
paths to form a quantile, and `sample_count=1` because anything larger is
averaged inside the model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.kronos_forecaster import (
    KronosUnavailable,
    load_kronos,
    make_kronos_forecaster,
)


def _bars(n: int = 512, tz: str | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz=tz)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.002, "Low": close * 0.998,
         "Close": close, "Volume": np.full(n, 1e6)},
        index=idx,
    )


class FakePredictor:
    """Records every call, and returns paths whose final close is controllable."""

    def __init__(self, finals=None, spread: float = 0.05):
        self.calls: list[dict] = []
        self.finals = finals
        self.spread = spread
        self._rng = np.random.default_rng(0)

    def predict_batch(self, *, df_list, x_timestamp_list, y_timestamp_list,
                      pred_len, T, top_k, top_p, sample_count, verbose):
        self.calls.append({
            "n": len(df_list), "pred_len": pred_len, "T": T, "top_k": top_k,
            "top_p": top_p, "sample_count": sample_count,
            "x_ts": x_timestamp_list[0], "y_ts": y_timestamp_list[0],
            "rows": len(df_list[0]),
        })
        last = float(df_list[0]["close"].iloc[-1])
        out = []
        for _ in df_list:
            final = (self.finals.pop(0) if self.finals
                     else last * (1.0 + self._rng.normal(0, self.spread)))
            closes = np.linspace(last, final, pred_len)
            out.append(pd.DataFrame(
                {"open": closes, "high": closes, "low": closes,
                 "close": closes, "volume": np.full(pred_len, 1e6)},
                index=y_timestamp_list[0]))
        return out


class TestItAsksTheRightQuestion:
    def test_it_never_truncates_the_tail_by_default(self):
        """top_p=0.9 would sample from a clipped distribution and then report
        its percentiles as the model's uncertainty."""
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=20, batch_size=20)(_bars(), 5, 0.90)
        assert p.calls[0]["top_p"] == 1.0
        assert p.calls[0]["top_k"] == 0

    def test_sample_count_is_always_one(self):
        """`sample_count` > 1 is averaged inside model/kronos.py, which would
        collapse every path into one line."""
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=40, batch_size=10)(_bars(), 5, 0.90)
        assert {c["sample_count"] for c in p.calls} == {1}

    def test_it_defaults_to_enough_paths_for_a_quantile(self):
        p = FakePredictor()
        make_kronos_forecaster(p, batch_size=50)(_bars(), 5, 0.90)
        assert sum(c["n"] for c in p.calls) == 200

    def test_a_path_count_that_cannot_form_a_band_is_refused(self):
        with pytest.raises(ValueError, match="at least 2"):
            make_kronos_forecaster(FakePredictor(), n_paths=1)

    def test_a_thin_path_count_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            make_kronos_forecaster(FakePredictor(), n_paths=20)
        assert any("n_paths=20" in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_an_impossible_top_p_is_refused(self, bad):
        with pytest.raises(ValueError, match="top_p"):
            make_kronos_forecaster(FakePredictor(), top_p=bad)


class TestBatching:
    def test_chunks_cover_exactly_n_paths(self):
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=55, batch_size=20)(_bars(), 5, 0.90)
        assert [c["n"] for c in p.calls] == [20, 20, 15]

    def test_the_band_spans_all_paths_not_just_the_last_chunk(self):
        # 100 paths across 4 chunks; the extremes live in the first and last.
        finals = [50.0] + [100.0] * 98 + [200.0]
        p = FakePredictor(finals=list(finals))
        band = make_kronos_forecaster(
            p, n_paths=100, batch_size=25)(_bars(), 5, 1.0)
        assert band.lower == pytest.approx(50.0, rel=1e-6)
        assert band.upper == pytest.approx(200.0, rel=1e-6)


class TestTimestamps:
    def test_a_tz_aware_index_is_converted_not_stripped(self):
        """Stripping keeps the wall clock, so the hour-of-day feature the model
        consumes would be shifted by the UTC offset."""
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=4, batch_size=4)(
            _bars(tz="America/New_York"), 5, 0.90)
        x_ts = p.calls[0]["x_ts"]
        assert x_ts.dt.tz is None
        naive = _bars(tz="America/New_York").index[-1]
        assert x_ts.iloc[-1] == naive.tz_convert("UTC").tz_localize(None)

    def test_future_stamps_run_forward_and_match_the_horizon(self):
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=4, batch_size=4)(_bars(), 7, 0.90)
        y_ts = p.calls[0]["y_ts"]
        assert len(y_ts) == 7 == p.calls[0]["pred_len"]
        assert y_ts.iloc[0] > p.calls[0]["x_ts"].iloc[-1]
        assert y_ts.is_monotonic_increasing

    def test_a_custom_calendar_is_honoured(self):
        """So a real trading calendar can replace BDay, which ignores holidays."""
        p = FakePredictor()
        make_kronos_forecaster(
            p, n_paths=4, batch_size=4, freq=pd.Timedelta(hours=1),
        )(_bars(), 3, 0.90)
        y_ts = p.calls[0]["y_ts"]
        assert (y_ts.diff().dropna() == pd.Timedelta(hours=1)).all()


class TestContextAndFailSoft:
    def test_the_window_is_capped_to_the_lookback(self):
        p = FakePredictor()
        make_kronos_forecaster(p, n_paths=4, batch_size=4, lookback=512)(
            _bars(n=3000), 5, 0.90)
        assert p.calls[0]["rows"] == 512

    def test_too_little_history_declines(self):
        assert make_kronos_forecaster(FakePredictor(), n_paths=4)(
            _bars(n=40), 5, 0.90) is None

    def test_a_non_finite_bar_declines_rather_than_feeding_nan(self):
        bars = _bars()
        bars.loc[bars.index[-3], "Close"] = np.nan
        assert make_kronos_forecaster(FakePredictor(), n_paths=4)(
            bars, 5, 0.90) is None

    def test_a_raising_predictor_declines_rather_than_propagating(self):
        class Boom:
            def predict_batch(self, **kw):
                raise RuntimeError("cuda gone")

        assert make_kronos_forecaster(Boom(), n_paths=4)(_bars(), 5, 0.90) is None

    def test_mostly_unusable_paths_decline_rather_than_quantile_the_scraps(self):
        finals = [float("nan")] * 95 + [100.0] * 5
        p = FakePredictor(finals=list(finals))
        assert make_kronos_forecaster(
            p, n_paths=100, batch_size=100)(_bars(), 5, 0.90) is None

    def test_the_band_is_a_valid_interval(self):
        band = make_kronos_forecaster(FakePredictor(), n_paths=100,
                                      batch_size=50)(_bars(), 5, 0.90)
        assert band is not None and band.valid()
        assert band.lower <= band.median <= band.upper


class TestMissingDependency:
    def test_a_missing_install_is_a_named_reason_not_a_traceback(self):
        with pytest.raises(KronosUnavailable) as e:
            load_kronos(kronos_path="/nonexistent/kronos/clone/xyz")
        msg = str(e.value)
        assert "torch" in msg or "Kronos" in msg


class TestFirewall:
    def test_the_adapter_cannot_reach_the_money_path(self):
        import pathlib
        src = pathlib.Path("src/research/kronos_forecaster.py").read_text()
        for forbidden in ("src.alpaca", "executor", "place_order",
                          "execute_trade_plan", "TradingClient", "generate_trade_plan"):
            assert forbidden not in src, forbidden
