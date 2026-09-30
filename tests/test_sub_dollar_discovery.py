"""Discovery reaches sub-$1 stocks — and only stocks.

NIVF ran $0.072 -> $0.25 (+244%) on 2026-09-30 and never entered the pipeline:
movers.min_price was $1.00. Lowering it alone would have been worse than not
lowering it — with the floor removed, 19 of the 40 top-scoring slots went to
sub-$1 names, mostly warrants and rights, and NIVFW rode in beside NIVF. So the
floor came down together with a derivative filter, in BOTH discovery paths,
and sub-$1 prices are shown to four decimals so the alert states the price
you would actually act on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import src.utils.config as cfg
from src.data import security_type as st
from src.scanner import movers as mv
from src.scanner import premarket_movers as pm
from src.scanner.movers_alerts import fmt_price, format_alert

_NAMES = {
    "NIVF": "NewGenIvf Group Limited Class A Ordinary Shares",
    "NIVFW": "NewGenIvf Group Limited Warrants",
    "ASTLW": "Algoma Steel Group Inc. Warrant",
    "ET": "Energy Transfer LP Common Units representing limited partner interests",
    "BIGW": "Some Acquisition Corp Warrants",
    "AAPL": "Apple Inc. Common Stock",
}

_FEED = {
    "gainers": [
        {"symbol": "NIVF", "price": 0.2477, "name": "NewGenIvf", "changesPercentage": 244.0},
        # Priced ABOVE the $0.05 floor on purpose, so it is the derivative
        # filter that removes it, not the price floor. (Its real 09-30 quote was
        # $0.0251 — the floor alone would have dropped it; many warrants sit
        # below a nickel.)
        {"symbol": "NIVFW", "price": 0.0600, "name": "NewGenIvf W", "changesPercentage": 172.8},
        {"symbol": "ET", "price": 17.20, "name": "Energy Transfer", "changesPercentage": 4.1},
        {"symbol": "AAPL", "price": 341.68, "name": "Apple", "changesPercentage": 3.4},
        # Past the corporate-action threshold: a derivative here must be dropped
        # BEFORE the guard spends a fetch verifying it.
        {"symbol": "BIGW", "price": 0.03, "name": "Big W", "changesPercentage": 900.0},
    ],
    "losers": [
        {"symbol": "ASTLW", "price": 0.0040, "name": "Algoma W", "changesPercentage": -59.2},
    ],
    "most_actives": [],
}


class _Resp:
    ok = True

    def raise_for_status(self):
        pass

    def json(self):
        return [{"symbol": s, "name": n} for s, n in _NAMES.items()]


class _Cache(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value, ttl=None):
        self[key] = value


@pytest.fixture(autouse=True)
def _asset_list(monkeypatch):
    """A real classifier over a stubbed asset list — no network, no disk."""
    monkeypatch.setattr(cfg, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(cfg, "ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(st, "cache", _Cache())
    monkeypatch.setattr(st, "_retry_after", 0.0)
    monkeypatch.setattr(st, "_last_fetch_ok", False)
    monkeypatch.setattr(st, "get_retry_session",
                        lambda: type("S", (), {"get": lambda self, url, **kw: _Resp()})())


def _discover(monkeypatch, **over):
    base = dict(MOVERS_SOURCES=["gainers", "losers", "most_actives"],
                MOVERS_PROVIDERS=["alpaca"], MOVERS_MIN_PRICE=0.05, MOVERS_MAX_PRICE=0.0,
                MOVERS_MIN_CHANGE_PCT=3.0, MOVERS_LIMIT=40, MOVERS_INCLUDE_SHORT=True,
                MOVERS_ENRICH_INTRADAY=False, MOVERS_SUSPECT_CHANGE_PCT=300.0,
                MOVERS_SUSPECT_AGREEMENT_PCT=25.0, MOVERS_EXCLUDE_DERIVATIVES=True)
    base.update(over)
    for k, v in base.items():
        monkeypatch.setattr(cfg, k, v)
    verified: list[str] = []
    monkeypatch.setattr(mv, "_fetch_source", lambda s: _FEED.get(s, []))
    monkeypatch.setattr(mv, "_verified_change_pct",
                        lambda t, p: verified.append(t) or None)
    monkeypatch.setattr(mv, "annotate_halts", lambda cs: cs)
    out = {c.ticker: c for c in mv.fetch_market_movers()}
    return out, mv.last_source_health(), verified


class TestRegularSessionDiscovery:
    def test_the_nivf_case_is_now_caught(self, monkeypatch):
        out, _, _ = _discover(monkeypatch)
        assert "NIVF" in out

    def test_its_warrant_is_not(self, monkeypatch):
        out, _, _ = _discover(monkeypatch)
        assert "NIVFW" not in out and "ASTLW" not in out

    def test_a_partnership_is_still_a_stock(self, monkeypatch):
        """The regression the classifier audit caught."""
        out, _, _ = _discover(monkeypatch)
        assert "ET" in out

    def test_exclusions_are_recorded_with_their_method(self, monkeypatch):
        _, health, _ = _discover(monkeypatch)
        assert set(health["excluded_derivatives"]) == {"NIVFW", "ASTLW", "BIGW"}
        # A string, not a list of its characters — the health copy used to
        # iterate every field.
        assert health["derivative_filter"] == "asset_names"

    def test_a_derivative_never_costs_a_verification_fetch(self, monkeypatch):
        _, _, verified = _discover(monkeypatch)
        assert "BIGW" not in verified

    def test_the_switch_turns_it_off(self, monkeypatch):
        out, health, verified = _discover(monkeypatch, MOVERS_EXCLUDE_DERIVATIVES=False)
        assert "NIVFW" in out
        assert health["excluded_derivatives"] == []
        assert health["derivative_filter"] is None
        # With the filter off, the +900% derivative DOES reach the guard — which
        # is what makes the "never costs a fetch" test above mean something.
        assert "BIGW" in verified

    def test_the_old_floor_would_have_missed_nivf(self, monkeypatch):
        out, _, _ = _discover(monkeypatch, MOVERS_MIN_PRICE=1.0)
        assert "NIVF" not in out and "ET" in out

    def test_the_shipped_config_is_the_lowered_one(self):
        """Read the real config.yaml values — not a monkeypatched stand-in."""
        import importlib
        import src.utils.config as live
        importlib.reload(live)
        try:
            assert live.MOVERS_MIN_PRICE <= 0.0636          # NIVF's low the day before
            assert live.PREMARKET_MOVERS_MIN_PRICE <= 0.0636
            assert live.MOVERS_EXCLUDE_DERIVATIVES is True
        finally:
            importlib.reload(live)


class TestPremarketDiscovery:
    """The 07:00-09:30 window the worker actually discovers from — NIVF did
    most of its run before the open."""

    def _run(self, monkeypatch, exclude=True):
        from src.scanner.premarket_movers import PremarketMover, PremarketProvider
        movers = [PremarketMover("NIVF", 0.12, 66.0, 0.072),
                  PremarketMover("NIVFW", 0.06, 150.0, 0.024),     # above the floor
                  PremarketMover("ET", 17.2, 4.5, 16.46)]
        prov = PremarketProvider("fake", lambda now=None: movers, "bounded_pool")
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_MIN_PRICE", 0.05)
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_MIN_CHANGE_PCT", 4.0)
        monkeypatch.setattr(cfg, "PREMARKET_MOVERS_LIMIT", 50)
        monkeypatch.setattr(cfg, "MOVERS_EXCLUDE_DERIVATIVES", exclude)
        monkeypatch.setattr(pm, "provider_chain", lambda names=None: [prov])
        now = datetime(2026, 9, 30, 12, 15, tzinfo=timezone.utc)   # 08:15 ET
        kept = [m.ticker for m in pm.fetch_premarket_movers(now=now)]
        return kept, pm.last_discovery_info()

    def test_the_premarket_floor_came_down_too(self, monkeypatch):
        kept, _ = self._run(monkeypatch)
        assert "NIVF" in kept

    def test_derivatives_are_excluded_here_as_well(self, monkeypatch):
        kept, info = self._run(monkeypatch)
        assert "NIVFW" not in kept and "ET" in kept
        assert info["excluded_derivatives"] == ["NIVFW"]
        assert info["derivative_filter"] == "asset_names"

    def test_and_the_same_switch_governs_it(self, monkeypatch):
        kept, _ = self._run(monkeypatch, exclude=False)
        assert "NIVFW" in kept


class TestPriceIsStatedToItsTick:
    @pytest.mark.parametrize("price,shown", [
        (0.2477, "$0.2477"), (0.0636, "$0.0636"), (0.0040, "$0.0040"),
        (1.0, "$1.00"), (341.68, "$341.68"), (1186.555, "$1,186.56"),
    ])
    def test_four_decimals_below_a_dollar_two_above(self, price, shown):
        assert fmt_price(price) == shown

    def test_the_alert_carries_the_real_price(self):
        c = mv.MoverCandidate(ticker="NIVF", price=0.2477, change_pct=244.0,
                              direction="long", score=85.0)
        msg = format_alert(c)
        assert "$0.2477" in msg and "$0.25 " not in msg

    @pytest.mark.parametrize("junk", [None, "x", object()])
    def test_junk_prices_do_not_raise(self, junk):
        assert fmt_price(junk) == "$--"

    def test_the_dashboard_helper_matches(self):
        """The same rule in the JS, and no movers cell bypassing it."""
        import pathlib
        src = pathlib.Path("dashboard.py").read_text()
        assert "Math.abs(amount) < 1 ? `$${amount.toFixed(4)}`" in src
        assert "Number(m.price).toFixed(2)" not in src
        assert "Number(w.live_price).toFixed(2)" not in src
