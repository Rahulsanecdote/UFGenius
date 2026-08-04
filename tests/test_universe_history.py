"""Tests for point-in-time universe membership in the backtest (audit M11)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.backtest import engine
from src.backtest.engine import backtest_signal_system
from src.backtest.universe_history import load_universe_history


def _write_history(tmp_path, payload) -> str:
    path = tmp_path / "universe_history.json"
    path.write_text(json.dumps(payload))
    return str(path)


class TestLoadUniverseHistory:
    def test_membership_intervals(self, tmp_path):
        path = _write_history(
            tmp_path,
            {
                "AAPL": [{"start": "2015-01-01", "end": None}],
                "TWTR": [{"start": "2015-01-01", "end": "2022-10-27"}],
            },
        )
        hist = load_universe_history(path)
        assert hist is not None and len(hist) == 2
        assert hist.is_member("AAPL", "2024-06-01")          # open-ended
        assert hist.is_member("twtr", "2022-10-27")          # inclusive end, case-insensitive
        assert not hist.is_member("TWTR", "2022-10-28")      # after delisting
        assert not hist.is_member("TWTR", "2014-12-31")      # before start
        assert not hist.is_member("MSFT", "2024-06-01")      # absent = never a member

    def test_multiple_intervals_rejoin(self, tmp_path):
        path = _write_history(
            tmp_path,
            {"AAA": [
                {"start": "2018-01-01", "end": "2019-06-30"},
                {"start": "2021-01-01", "end": None},
            ]},
        )
        hist = load_universe_history(path)
        assert hist.is_member("AAA", "2019-06-30")
        assert not hist.is_member("AAA", "2020-06-01")  # gap between memberships
        assert hist.is_member("AAA", "2022-01-01")

    def test_no_path_returns_none(self):
        assert load_universe_history(None) is None
        assert load_universe_history("") is None

    def test_missing_file_fails_open_with_none(self, tmp_path):
        assert load_universe_history(str(tmp_path / "nope.json")) is None

    @pytest.mark.parametrize(
        "payload",
        [
            ["not", "a", "dict"],
            {"AAA": {"start": "2020-01-01"}},                       # not a list
            {"AAA": [{"end": "2020-01-01"}]},                       # missing start
            {"AAA": [{"start": "garbage"}]},                        # unparseable date
            {"AAA": [{"start": None}]},                             # null start
            {"AAA": [{"start": "2022-01-01", "end": "2020-01-01"}]},  # end < start
        ],
    )
    def test_malformed_payloads_return_none(self, tmp_path, payload):
        assert load_universe_history(_write_history(tmp_path, payload)) is None

    def test_invalid_json_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert load_universe_history(str(path)) is None


class TestBacktestPointInTimeGate:
    def _frame(self):
        dates = pd.date_range("2024-01-01", periods=5, freq="D")
        return pd.DataFrame(
            {
                "Open": [100.0, 100.0, 100.0, 100.0, 100.0],
                "Close": [100.0, 100.5, 101.0, 101.5, 102.0],
                "ATR_14": [2.0] * 5,
                "entry_signal": [True, False, False, False, False],
            },
            index=dates,
        )

    def test_non_member_on_entry_date_takes_no_trade(self, tmp_path, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_a, **_k: frame)
        # AAA left the universe before the 2024-01-02 fill date.
        hist = load_universe_history(
            _write_history(tmp_path, {"AAA": [{"start": "2020-01-01", "end": "2023-12-31"}]})
        )
        result = backtest_signal_system(
            ["AAA"], "2024-01-01", "2024-01-05",
            initial_capital=10_000, universe_history=hist,
        )
        assert result["trades"] == []
        assert result["point_in_time_universe"] is True

    def test_member_on_entry_date_trades(self, tmp_path, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_a, **_k: frame)
        hist = load_universe_history(
            _write_history(tmp_path, {"AAA": [{"start": "2020-01-01", "end": None}]})
        )
        result = backtest_signal_system(
            ["AAA"], "2024-01-01", "2024-01-05",
            initial_capital=10_000, universe_history=hist,
        )
        assert result["trades"], "Member ticker should trade"
        assert result["trades"][0]["entry_date"] == "2024-01-02"

    def test_disclosure_switches_with_history(self, tmp_path, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_a, **_k: frame)

        without = backtest_signal_system(["AAA"], "2024-01-01", "2024-01-05")
        assert without["point_in_time_universe"] is False
        assert any(
            d.startswith("SURVIVORSHIP:") for d in without["bias_disclosures"]
        )

        hist = load_universe_history(
            _write_history(tmp_path, {"AAA": [{"start": "2020-01-01", "end": None}]})
        )
        with_hist = backtest_signal_system(
            ["AAA"], "2024-01-01", "2024-01-05", universe_history=hist
        )
        assert with_hist["point_in_time_universe"] is True
        assert any(
            d.startswith("SURVIVORSHIP (mitigated)") for d in with_hist["bias_disclosures"]
        )

    def test_config_path_fallback(self, tmp_path, monkeypatch):
        frame = self._frame()
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_a, **_k: frame)
        path = _write_history(tmp_path, {"AAA": [{"start": "2020-01-01", "end": None}]})
        monkeypatch.setattr(engine.config, "BACKTEST_UNIVERSE_HISTORY_PATH", path)

        result = backtest_signal_system(["AAA"], "2024-01-01", "2024-01-05")
        assert result["point_in_time_universe"] is True
        assert result["trades"]
