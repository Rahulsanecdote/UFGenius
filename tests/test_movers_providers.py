"""Tests for the movers provider chain (src/scanner/movers_providers.py).

Hermetic: the HTTP boundary is mocked. The behaviour that matters is the
None-vs-list contract — an EMPTY answer is a real answer (a quiet market) and
must stop the chain, while "could not answer" must fall through. Getting that
backwards would either hide an outage or treat quiet as broken.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.scanner import movers as mv
from src.scanner import movers_providers as mp
from src.utils import config as cfg


def _session(payload=None, boom=False, status_boom=False):
    def _get(url, **kwargs):
        if boom:
            raise RuntimeError("network down")
        resp = MagicMock()
        resp.raise_for_status.side_effect = (
            RuntimeError("http 403") if status_boom else None)
        resp.json.return_value = payload
        return resp
    session = MagicMock()
    session.get.side_effect = _get
    return session


def _with(monkeypatch, session, **cfgover):
    for key, value in cfgover.items():
        monkeypatch.setattr(cfg, key, value)
    monkeypatch.setattr(mp, "get_retry_session", lambda: session)


# ── adapter normalisation ────────────────────────────────────────────────────

class TestFmpAdapter:
    def test_normalises_rows(self, monkeypatch):
        _with(monkeypatch, _session([
            {"symbol": "aaa", "price": "10.5", "changesPercentage": "7.25",
             "name": "Aaa Co"},
        ]), FMP_KEY="k")
        assert mp.fetch_fmp("gainers") == [
            {"symbol": "AAA", "price": 10.5, "changesPercentage": 7.25,
             "name": "Aaa Co"}]

    def test_non_list_payload_is_a_failure_not_an_empty_answer(self, monkeypatch):
        # An exhausted quota arrives as HTTP 200 + a JSON object.
        _with(monkeypatch, _session({"Error Message": "Limit Reach..."}), FMP_KEY="k")
        assert mp.fetch_fmp("gainers") is None

    def test_empty_list_is_a_real_answer(self, monkeypatch):
        _with(monkeypatch, _session([]), FMP_KEY="k")
        assert mp.fetch_fmp("gainers") == []

    def test_rows_missing_essentials_are_dropped(self, monkeypatch):
        _with(monkeypatch, _session([
            {"symbol": "AAA", "price": 10.0, "changesPercentage": 5.0},
            {"symbol": "", "price": 1.0, "changesPercentage": 5.0},     # no symbol
            {"symbol": "BBB", "changesPercentage": 5.0},                 # no price
            {"symbol": "CCC", "price": 1.0},                             # no change
        ]), FMP_KEY="k")
        assert [r["symbol"] for r in mp.fetch_fmp("gainers")] == ["AAA"]

    def test_no_key_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session([]), FMP_KEY="")
        assert mp.fetch_fmp("gainers") is None


class TestAlpacaAdapter:
    def test_reads_the_requested_side(self, monkeypatch):
        payload = {
            "gainers": [{"symbol": "UP", "price": 3.0, "percent_change": 25.0}],
            "losers": [{"symbol": "DOWN", "price": 2.0, "percent_change": -18.0}],
        }
        _with(monkeypatch, _session(payload), ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        assert mp.fetch_alpaca("gainers")[0]["symbol"] == "UP"
        assert mp.fetch_alpaca("losers")[0]["changesPercentage"] == -18.0

    def test_most_actives_is_unsupported(self, monkeypatch):
        # Alpaca's most-actives rows carry volume/trade count but no price, so
        # there is nothing to build a candidate from.
        _with(monkeypatch, _session({}), ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        assert mp.fetch_alpaca("most_actives") is None

    def test_a_plan_without_the_screener_cannot_answer(self, monkeypatch):
        # A tier without screener access answers 403 — a normal "cannot serve".
        _with(monkeypatch, _session(status_boom=True),
              ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        assert mp.fetch_alpaca("gainers") is None

    def test_no_keys_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session({}), ALPACA_API_KEY="", ALPACA_SECRET_KEY="")
        assert mp.fetch_alpaca("gainers") is None


class TestPolygonAdapter:
    def test_prefers_last_trade_over_the_day_bar(self, monkeypatch):
        _with(monkeypatch, _session({"tickers": [
            {"ticker": "AAA", "todaysChangePerc": 9.5,
             "day": {"c": 10.0}, "lastTrade": {"p": 10.4}},
        ]}), POLYGON_KEY="k")
        assert mp.fetch_polygon("gainers")[0]["price"] == 10.4

    def test_falls_back_to_the_day_close(self, monkeypatch):
        _with(monkeypatch, _session({"tickers": [
            {"ticker": "AAA", "todaysChangePerc": 9.5, "day": {"c": 10.0}},
        ]}), POLYGON_KEY="k")
        assert mp.fetch_polygon("gainers")[0]["price"] == 10.0

    def test_most_actives_is_unsupported(self, monkeypatch):
        _with(monkeypatch, _session({"tickers": []}), POLYGON_KEY="k")
        assert mp.fetch_polygon("most_actives") is None

    def test_unexpected_payload_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session({"results": []}), POLYGON_KEY="k")
        assert mp.fetch_polygon("gainers") is None


# ── the chain ────────────────────────────────────────────────────────────────

def _fake(name, supports, result, configured=True):
    calls = []

    def _fetch(source):
        calls.append(source)
        if isinstance(result, Exception):
            raise result
        return result

    provider = mp.MoversProvider(name, _fetch, frozenset(supports), lambda: configured)
    return provider, calls


def _discover(monkeypatch, chain, source="gainers"):
    monkeypatch.setattr(mv, "provider_chain", lambda *a, **k: chain)
    monkeypatch.setattr(cfg, "MOVERS_HALTS_ENABLED", False)
    monkeypatch.setattr(cfg, "MOVERS_SOURCES", [source])
    monkeypatch.setattr(cfg, "MOVERS_ENRICH_INTRADAY", False)
    monkeypatch.setattr(cfg, "MOVERS_MIN_CHANGE_PCT", 3.0)
    monkeypatch.setattr(cfg, "MOVERS_MIN_PRICE", 1.0)
    monkeypatch.setattr(cfg, "MOVERS_MAX_PRICE", 0.0)
    return mv.fetch_market_movers()


_ROW = [{"symbol": "AAA", "price": 10.0, "changesPercentage": 20.0, "name": ""}]


class TestChain:
    def test_first_provider_that_answers_wins(self, monkeypatch):
        first, first_calls = _fake("first", ["gainers"], _ROW)
        second, second_calls = _fake("second", ["gainers"], _ROW)
        out = _discover(monkeypatch, [first, second])
        assert [c.ticker for c in out] == ["AAA"]
        assert first_calls == ["gainers"] and second_calls == []   # never reached
        assert mv.last_source_health()["served_by"] == {"gainers": "first"}

    def test_falls_through_when_a_provider_cannot_answer(self, monkeypatch):
        dead, dead_calls = _fake("dead", ["gainers"], None)
        alive, alive_calls = _fake("alive", ["gainers"], _ROW)
        out = _discover(monkeypatch, [dead, alive])
        assert [c.ticker for c in out] == ["AAA"]
        assert dead_calls == ["gainers"] and alive_calls == ["gainers"]
        assert mv.last_source_health()["served_by"] == {"gainers": "alive"}

    def test_an_empty_answer_stops_the_chain(self, monkeypatch):
        # A quiet market is an ANSWER. Falling through here would let a
        # genuinely empty list look like a broken provider.
        quiet, _ = _fake("quiet", ["gainers"], [])
        backup, backup_calls = _fake("backup", ["gainers"], _ROW)
        out = _discover(monkeypatch, [quiet, backup])
        assert out == []
        assert backup_calls == []
        health = mv.last_source_health()
        assert health["succeeded"] == ["gainers"] and health["failed"] == []

    def test_a_provider_that_raises_falls_through(self, monkeypatch):
        broken, _ = _fake("broken", ["gainers"], RuntimeError("adapter bug"))
        alive, _ = _fake("alive", ["gainers"], _ROW)
        assert [c.ticker for c in _discover(monkeypatch, [broken, alive])] == ["AAA"]

    def test_unsupported_sources_are_skipped_not_failed(self, monkeypatch):
        narrow, narrow_calls = _fake("narrow", ["losers"], _ROW)
        wide, _ = _fake("wide", ["gainers"], _ROW)
        _discover(monkeypatch, [narrow, wide])
        assert narrow_calls == []                       # never asked
        assert mv.last_source_health()["served_by"] == {"gainers": "wide"}

    def test_unconfigured_providers_are_skipped(self, monkeypatch):
        keyless, keyless_calls = _fake("keyless", ["gainers"], _ROW, configured=False)
        alive, _ = _fake("alive", ["gainers"], _ROW)
        _discover(monkeypatch, [keyless, alive])
        assert keyless_calls == []
        assert mv.last_source_health()["served_by"] == {"gainers": "alive"}

    def test_nothing_configured_is_reported_differently_from_everything_failing(
            self, monkeypatch):
        keyless, _ = _fake("keyless", ["gainers"], _ROW, configured=False)
        _discover(monkeypatch, [keyless])
        assert mv.last_source_health()["failed"] == ["gainers: no_provider_configured"]

        dead, _ = _fake("dead", ["gainers"], None)
        _discover(monkeypatch, [dead])
        assert mv.last_source_health()["failed"] == ["gainers: no_provider_answered"]

    def test_empty_chain_fails_the_source(self, monkeypatch):
        assert _discover(monkeypatch, []) == []
        assert mv.last_source_health()["failed"] == ["gainers: no_provider_configured"]


class TestProviderChainResolution:
    def test_resolves_names_in_order(self):
        assert [p.name for p in mp.provider_chain(["fmp", "alpaca"])] == ["fmp", "alpaca"]

    def test_unknown_names_are_skipped(self):
        assert [p.name for p in mp.provider_chain(["nope", "fmp"])] == ["fmp"]

    @pytest.mark.parametrize("name,sources", [
        ("alpaca", {"gainers", "losers"}),
        ("polygon", {"gainers", "losers"}),
        ("fmp", {"gainers", "losers", "most_actives"}),
    ])
    def test_declared_support(self, name, sources):
        assert set(mp.provider_chain([name])[0].supports) == sources


class TestUnusablePayloadFallsThrough:
    """A payload with rows but none usable is a schema/entitlement change.

    Returning [] there would read as a quiet market and STOP the chain, so the
    fallback would never run (Codex P2). Partial damage still yields the good
    rows — only a total loss counts as "could not answer".
    """

    def test_fmp_rows_all_malformed_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session([{"sym": "AAA", "pct": 5.0}] * 3), FMP_KEY="k")
        assert mp.fetch_fmp("gainers") is None

    def test_alpaca_rows_all_malformed_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session({"gainers": [{"symbol": "UP"}] * 3}),
              ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        assert mp.fetch_alpaca("gainers") is None

    def test_polygon_rows_all_malformed_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session({"tickers": [{"ticker": "AAA"}] * 3}),
              POLYGON_KEY="k")
        assert mp.fetch_polygon("gainers") is None

    def test_partial_damage_keeps_the_usable_rows(self, monkeypatch):
        _with(monkeypatch, _session([
            {"symbol": "AAA", "price": 10.0, "changesPercentage": 5.0},
            {"symbol": "BAD"},
        ]), FMP_KEY="k")
        assert [r["symbol"] for r in mp.fetch_fmp("gainers")] == ["AAA"]

    def test_an_unusable_provider_falls_through_to_the_next(self, monkeypatch):
        # End to end: Alpaca answers with rows it cannot parse, FMP serves.
        broken = mp.MoversProvider(
            "broken", lambda s: mp._normalise([{"junk": 1}], lambda it: None),
            frozenset({"gainers"}), lambda: True)
        good, good_calls = _fake("good", ["gainers"], _ROW)
        out = _discover(monkeypatch, [broken, good])
        assert [c.ticker for c in out] == ["AAA"]
        assert good_calls == ["gainers"]
        assert mv.last_source_health()["served_by"] == {"gainers": "good"}


class TestNonFiniteNumbers:
    """float() accepts NaN and infinity, and NaN defeats every comparison.

    A NaN price would make an unusable payload look usable (stopping the chain
    instead of falling through) and then slip past the min/max price filters,
    since `nan < 1.0` and `nan > 100.0` are both False (CodeRabbit).
    """

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf")])
    def test_non_finite_values_are_rejected(self, bad):
        assert mp._num(bad) is None

    def test_a_non_finite_price_makes_the_row_unusable(self):
        assert mp._row("AAA", float("nan"), 5.0) is None
        assert mp._row("AAA", 10.0, float("inf")) is None

    def test_a_payload_of_non_finite_rows_cannot_answer(self, monkeypatch):
        _with(monkeypatch, _session([
            {"symbol": "AAA", "price": "NaN", "changesPercentage": 5.0},
        ]), FMP_KEY="k")
        assert mp.fetch_fmp("gainers") is None

    def test_a_non_finite_row_never_reaches_the_ranked_list(self, monkeypatch):
        mixed, _ = _fake("mixed", ["gainers"], [
            {"symbol": "GOOD", "price": 10.0, "changesPercentage": 20.0, "name": ""},
            {"symbol": "NANP", "price": float("nan"), "changesPercentage": 20.0,
             "name": ""},
        ])
        out = _discover(monkeypatch, [mixed])
        assert [c.ticker for c in out] == ["GOOD"]


def test_an_unknown_source_is_recorded_as_a_failure(monkeypatch):
    # A typo in movers.sources must surface as a config error, not as a quiet
    # market: with no health recorded the dashboard reports available:true.
    monkeypatch.setattr(cfg, "MOVERS_SOURCES", ["typo_source"])
    monkeypatch.setattr(cfg, "MOVERS_HALTS_ENABLED", False)
    monkeypatch.setattr(cfg, "MOVERS_ENRICH_INTRADAY", False)
    assert mv.fetch_market_movers() == []
    health = mv.last_source_health()
    assert health["failed"] == ["typo_source: unknown_source"]
    assert health["succeeded"] == []
