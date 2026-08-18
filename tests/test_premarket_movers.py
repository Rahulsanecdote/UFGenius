"""Tests for pre-market movers discovery (src/scanner/premarket_movers.py).

Fully offline: every provider's HTTP call is stubbed with a captured-shape
payload. The behaviour under test is the part that is easy to get quietly
wrong — the list-vs-None contract that decides whether the chain falls through,
the staleness guard that stops last night's bars reading as this morning's tape,
and the coverage disclosure that says whether a fresh gapper could have appeared
at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.scanner import premarket_movers as pm
from src.utils import config as cfg

_NOW = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)   # 08:30 ET


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _poly_row(ticker, last, prev, *, age_sec=60.0, volume=250_000):
    ts_ns = (_NOW - timedelta(seconds=age_sec)).timestamp() * 1e9
    return {"ticker": ticker,
            "min": {"c": last, "v": volume, "t": ts_ns},
            "prevDay": {"c": prev}}


def _yahoo_quote(symbol, pre_price, pre_chg):
    return {"symbol": symbol, "preMarketPrice": pre_price,
            "preMarketChangePercent": pre_chg,
            "regularMarketPreviousClose": 999.0}   # deliberately wrong: unused


@pytest.fixture(autouse=True)
def _reset_thread_state():
    pm._state.info = None
    yield


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(cfg, "PREMARKET_MOVERS_MIN_PRICE", 1.0)
    monkeypatch.setattr(cfg, "PREMARKET_MOVERS_MIN_CHANGE_PCT", 4.0)
    monkeypatch.setattr(cfg, "PREMARKET_MOVERS_LIMIT", 50)
    monkeypatch.setattr(cfg, "PREMARKET_MOVERS_YAHOO_SCREENERS", ["day_gainers"])


class TestPolygon:
    def test_computes_change_from_the_live_minute_bar(self, monkeypatch):
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        payload = {"tickers": [_poly_row("AAA", 12.0, 10.0)]}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            out = pm.fetch_polygon(now=_NOW)
        assert [(m.ticker, round(m.change_pct, 1)) for m in out] == [("AAA", 20.0)]

    def test_stale_minute_bars_are_dropped(self, monkeypatch):
        # Last night's aggregate is not this morning's tape. A ticker that has
        # not traded pre-market is an answer ("quiet"), not a gap.
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        payload = {"tickers": [
            _poly_row("FRESH", 12.0, 10.0, age_sec=60),
            _poly_row("STALE", 30.0, 10.0, age_sec=60_000),
        ]}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            out = pm.fetch_polygon(now=_NOW)
        assert [m.ticker for m in out] == ["FRESH"]

    def test_rows_without_a_timestamp_are_dropped(self, monkeypatch):
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        payload = {"tickers": [{"ticker": "AAA", "min": {"c": 12.0},
                                "prevDay": {"c": 10.0}}]}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            assert pm.fetch_polygon(now=_NOW) is None   # nothing usable → failure

    def test_no_key_cannot_answer(self, monkeypatch):
        monkeypatch.setattr(cfg, "POLYGON_KEY", "")
        assert pm.fetch_polygon(now=_NOW) is None

    def test_http_error_cannot_answer(self, monkeypatch):
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp({}, status=403)
            assert pm.fetch_polygon(now=_NOW) is None

    def test_genuinely_empty_snapshot_is_a_real_answer(self, monkeypatch):
        # No rows at all is "the market is asleep" — an answer that must stop
        # the chain, not fall through to a narrower provider.
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp({"tickers": []})
            assert pm.fetch_polygon(now=_NOW) == []

    def test_non_finite_values_are_refused(self, monkeypatch):
        # NaN defeats every downstream comparison: `nan >= min_price` is False,
        # but so is `nan < min_price`, so a NaN row slips past filters intact.
        monkeypatch.setattr(cfg, "POLYGON_KEY", "k")
        payload = {"tickers": [_poly_row("NAN", float("nan"), 10.0),
                               _poly_row("OK", 12.0, 10.0)]}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            out = pm.fetch_polygon(now=_NOW)
        assert [m.ticker for m in out] == ["OK"]


class TestYahoo:
    def test_derives_prev_close_from_the_stated_change(self, _cfg):
        # regularMarketPreviousClose is the close BEFORE the last session, so
        # using it would misprice every name that moved yesterday. The prior
        # close is recovered from the percentage Yahoo actually quotes against.
        payload = {"finance": {"result": [{"quotes": [
            _yahoo_quote("AAA", 11.0, 10.0)]}]}}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            out = pm.fetch_yahoo(now=_NOW)
        assert len(out) == 1
        assert round(out[0].prev_close, 4) == 10.0
        assert round(out[0].change_pct, 4) == 10.0

    def test_quotes_without_a_premarket_print_are_skipped(self, _cfg):
        payload = {"finance": {"result": [{"quotes": [
            {"symbol": "QUIET", "regularMarketPrice": 10.0},
            _yahoo_quote("AAA", 11.0, 10.0)]}]}}
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.return_value = _Resp(payload)
            out = pm.fetch_yahoo(now=_NOW)
        assert [m.ticker for m in out] == ["AAA"]

    def test_total_failure_cannot_answer(self, _cfg):
        with patch.object(pm, "get_retry_session") as sess:
            sess.return_value.get.side_effect = RuntimeError("boom")
            assert pm.fetch_yahoo(now=_NOW) is None


class TestChain:
    def _serve(self, monkeypatch, *, polygon, yahoo):
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_PROVIDERS", ["polygon", "yahoo"])
        monkeypatch.setattr(pm, "_PROVIDERS", {
            "polygon": pm.PremarketProvider("polygon", lambda **k: polygon,
                                            pm.COVERAGE_MARKET_WIDE,
                                            lambda: True, "full snapshot"),
            "yahoo": pm.PremarketProvider("yahoo", lambda **k: yahoo,
                                          pm.COVERAGE_BOUNDED_POOL,
                                          lambda: True, "prior-session pool"),
        })

    def test_first_provider_that_answers_serves(self, monkeypatch, _cfg):
        self._serve(monkeypatch,
                    polygon=[pm.PremarketMover("AAA", 12.0, 20.0, 10.0)],
                    yahoo=[pm.PremarketMover("ZZZ", 12.0, 20.0, 10.0)])
        assert [m.ticker for m in pm.fetch_premarket_movers(now=_NOW)] == ["AAA"]
        assert pm.last_discovery_info()["coverage"] == pm.COVERAGE_MARKET_WIDE

    def test_falls_through_and_records_the_narrower_coverage(self, monkeypatch, _cfg):
        # The fallback is not equivalent: it changes what the list COULD contain,
        # so the coverage class has to change with it.
        self._serve(monkeypatch, polygon=None,
                    yahoo=[pm.PremarketMover("ZZZ", 12.0, 20.0, 10.0)])
        assert [m.ticker for m in pm.fetch_premarket_movers(now=_NOW)] == ["ZZZ"]
        info = pm.last_discovery_info()
        assert info["served_by"] == "yahoo"
        assert info["coverage"] == pm.COVERAGE_BOUNDED_POOL
        assert info["reason"] == "prior-session pool"    # the provider's own note

    def test_empty_answer_stops_the_chain(self, monkeypatch, _cfg):
        self._serve(monkeypatch, polygon=[],
                    yahoo=[pm.PremarketMover("ZZZ", 12.0, 20.0, 10.0)])
        assert pm.fetch_premarket_movers(now=_NOW) == []
        assert pm.last_discovery_info()["served_by"] == "polygon"

    def test_nothing_answered_is_distinguishable_from_nothing_configured(
            self, monkeypatch, _cfg):
        self._serve(monkeypatch, polygon=None, yahoo=None)
        assert pm.fetch_premarket_movers(now=_NOW) == []
        assert pm.last_discovery_info()["reason"] == "no_provider_answered"

        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_PROVIDERS", [])
        assert pm.fetch_premarket_movers(now=_NOW) == []
        assert pm.last_discovery_info()["reason"] == "no_provider_configured"

    def test_both_directions_ranked_by_absolute_move(self, monkeypatch, _cfg):
        # The screener gates on abs(gap_pct), so a -40% gap is as much a
        # candidate as a +40% one.
        self._serve(monkeypatch, polygon=[
            pm.PremarketMover("UP", 12.0, 20.0, 10.0),
            pm.PremarketMover("DOWN", 6.0, -40.0, 10.0),
            pm.PremarketMover("FLAT", 10.1, 1.0, 10.0),
        ], yahoo=None)
        assert [m.ticker for m in pm.fetch_premarket_movers(now=_NOW)] == ["DOWN", "UP"]

    def test_price_floor_applies(self, monkeypatch, _cfg):
        self._serve(monkeypatch, polygon=[
            pm.PremarketMover("SUBDOLLAR", 0.4, 60.0, 0.25),
            pm.PremarketMover("OK", 12.0, 20.0, 10.0),
        ], yahoo=None)
        assert [m.ticker for m in pm.fetch_premarket_movers(now=_NOW)] == ["OK"]

    def test_unknown_provider_is_skipped_loudly(self, monkeypatch, _cfg):
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_PROVIDERS", ["nope"])
        assert pm.provider_chain() == []

    def test_universe_helper_returns_plain_tickers(self, monkeypatch, _cfg):
        self._serve(monkeypatch,
                    polygon=[pm.PremarketMover("AAA", 12.0, 20.0, 10.0)], yahoo=None)
        assert pm.get_premarket_universe() == ["AAA"]


class TestSessionAwareness:
    """Zero movers means three different things; the count alone can't say which."""

    _IN = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)    # Tue 08:30 ET
    _AFTER = datetime(2026, 8, 18, 17, 0, tzinfo=timezone.utc)  # Tue 13:00 ET
    _WEEKEND = datetime(2026, 8, 16, 12, 30, tzinfo=timezone.utc)  # Sun 08:30 ET

    def test_session_window(self):
        assert pm.in_premarket_session(self._IN) is True
        assert pm.in_premarket_session(self._AFTER) is False
        assert pm.in_premarket_session(self._WEEKEND) is False

    def test_empty_outside_the_session_says_so(self, monkeypatch, _cfg):
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_PROVIDERS", ["yahoo"])
        monkeypatch.setattr(pm, "_PROVIDERS", {
            "yahoo": pm.PremarketProvider("yahoo", lambda **k: [],
                                          pm.COVERAGE_BOUNDED_POOL,
                                          lambda: True, "pool note"),
        })
        assert pm.fetch_premarket_movers(now=self._AFTER) == []
        info = pm.last_discovery_info()
        assert info["in_session"] is False
        assert "Outside the 04:00–09:30 ET" in info["reason"]

    def test_empty_inside_the_session_is_reported_as_a_quiet_tape(
            self, monkeypatch, _cfg):
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_PROVIDERS", ["yahoo"])
        monkeypatch.setattr(pm, "_PROVIDERS", {
            "yahoo": pm.PremarketProvider("yahoo", lambda **k: [],
                                          pm.COVERAGE_BOUNDED_POOL,
                                          lambda: True, "pool note"),
        })
        assert pm.fetch_premarket_movers(now=self._IN) == []
        info = pm.last_discovery_info()
        assert info["in_session"] is True
        assert "Outside" not in info["reason"]
