"""The worker discovers from the session it is actually in.

Before 09:30 the regular movers chain answers about YESTERDAY's session — the
staleness `premarket_movers` was written for, and the one
`alerts.require_fresh_session` refuses. So the 07:00–09:30 window discovered a
list the alerter then correctly threw away, and silence was the right
behaviour. This routes that window to the live extended-hours tape instead.

Hermetic: no network. The pre-market fetcher and the intraday fetch are stubbed.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import src.utils.config as cfg
from src.scanner import movers as mv
from src.scanner import movers_worker as mw
from src.scanner.premarket_movers import PremarketMover

# 08:15 ET on a Monday (12:15 UTC) — inside the pre-market window AND inside the
# worker's 07:00 scan-window start.
PREMARKET = datetime(2026, 9, 28, 12, 15, tzinfo=timezone.utc)
# 11:00 ET the same day — regular session.
REGULAR = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)

_PM = [PremarketMover(ticker="GAPR", price=6.0, change_pct=50.0, prev_close=4.0,
                      volume=120_000)]


class TestSourceSelection:
    def _route(self, now, enabled=True):
        seen = []
        with patch.object(cfg, "MOVERS_PREMARKET_DISCOVERY_ENABLED", enabled), \
             patch.object(mw, "fetch_market_movers",
                          lambda *a, **k: seen.append("regular") or []), \
             patch.object(mw, "fetch_premarket_candidates",
                          lambda *a, **k: seen.append("premarket") or []):
            mw._discover_for_now(now)
        return seen[0]

    def test_premarket_window_uses_the_extended_hours_tape(self):
        assert self._route(PREMARKET) == "premarket"

    def test_regular_session_uses_the_regular_chain(self):
        assert self._route(REGULAR) == "regular"

    def test_the_switch_turns_it_off(self):
        assert self._route(PREMARKET, enabled=False) == "regular"

    def test_an_empty_premarket_list_does_not_fall_back(self):
        """The regression this guards: substituting the regular chain when
        pre-market is quiet is EXACTLY how yesterday's list reached the morning
        window. An empty extended-hours list is a real answer."""
        seen = []
        with patch.object(cfg, "MOVERS_PREMARKET_DISCOVERY_ENABLED", True), \
             patch.object(mw, "fetch_market_movers",
                          lambda *a, **k: seen.append("regular") or []), \
             patch.object(mw, "fetch_premarket_candidates",
                          lambda *a, **k: seen.append("premarket") or []):
            out = mw._discover_for_now(PREMARKET)
        assert seen == ["premarket"]          # regular never consulted
        assert out == []

    def test_a_clock_fault_does_not_take_discovery_down(self):
        seen = []
        with patch.object(cfg, "MOVERS_PREMARKET_DISCOVERY_ENABLED", True), \
             patch("src.scanner.premarket_movers.in_premarket_session",
                   side_effect=RuntimeError("tz blew up")), \
             patch.object(mw, "fetch_market_movers",
                          lambda *a, **k: seen.append("regular") or []):
            mw._discover_for_now(PREMARKET)
        assert seen == ["regular"]


class TestCandidateAdapter:
    def _fetch(self, movers, info, enrich=False):
        with patch("src.scanner.premarket_movers.fetch_premarket_movers",
                   return_value=movers), \
             patch("src.scanner.premarket_movers.last_discovery_info",
                   return_value=info), \
             patch.object(cfg, "MOVERS_HALTS_ENABLED", False):
            return mv.fetch_premarket_candidates(enrich=enrich, now=PREMARKET)

    def test_movers_become_scored_candidates(self):
        out = self._fetch(_PM, {"served_by": "polygon", "coverage": "market_wide"})
        assert [c.ticker for c in out] == ["GAPR"]
        assert out[0].direction == "long" and out[0].sources == ["premarket"]
        assert out[0].score > 0

    def test_a_negative_gap_is_a_short_candidate(self):
        down = [PremarketMover(ticker="DROP", price=2.0, change_pct=-40.0,
                               prev_close=3.33)]
        assert self._fetch(down, {"served_by": "yahoo",
                                  "coverage": "bounded_pool"})[0].direction == "short"

    def test_coverage_is_disclosed_not_swallowed(self):
        """bounded_pool cannot see a name that was quiet yesterday, so which
        provider served changes what the list COULD contain."""
        self._fetch(_PM, {"served_by": "yahoo", "coverage": "bounded_pool"})
        assert mv.last_source_health()["served_by"]["premarket"] == "yahoo (bounded_pool)"

    def test_discovery_failure_is_recorded_not_raised(self):
        with patch("src.scanner.premarket_movers.fetch_premarket_movers",
                   side_effect=RuntimeError("provider down")):
            assert mv.fetch_premarket_candidates(now=PREMARKET) == []
        assert "premarket: discovery_error" in mv.last_source_health()["failed"]


def test_enrichment_requests_extended_hours_bars():
    """The interaction that makes or breaks this.

    Without prepost the fetch returns REGULAR-session bars, which before the
    open are yesterday's — so every candidate would be enriched with real
    metrics describing a finished session, and `require_fresh_session` would
    suppress the entire list as stale_session_data. Discovered, then thrown
    away, which is worse than quiet because it looks like the gate is broken.
    """
    calls = {}
    idx = pd.DatetimeIndex([datetime(2026, 9, 28, 12, 0),
                            datetime(2026, 9, 28, 12, 5)])
    frame = pd.DataFrame({"Open": [5.0, 5.5], "High": [5.2, 5.7],
                          "Low": [4.9, 5.4], "Close": [5.1, 5.6],
                          "Volume": [1000, 4000]}, index=idx)

    def _fake_intraday(ticker, interval=None, prepost=False, **kw):
        calls["prepost"] = prepost
        return frame

    with patch("src.scanner.premarket_movers.fetch_premarket_movers",
               return_value=_PM), \
         patch("src.scanner.premarket_movers.last_discovery_info",
               return_value={"served_by": "polygon", "coverage": "market_wide"}), \
         patch("src.data.fetcher.fetch_intraday", _fake_intraday), \
         patch.object(cfg, "MOVERS_HALTS_ENABLED", False):
        mv.fetch_premarket_candidates(enrich=True, now=PREMARKET)

    assert calls.get("prepost") is True


class TestBarsAsOfTimezone:
    """`bars_as_of` must be naive UTC whatever tz the provider sent.

    Measured 2026-09-28: `fetch_intraday` documents a naive-UTC index but does
    not enforce one — `sanitize_intraday` converts the tz only inside its own
    comparisons and returns the frame with the provider's index intact, and
    yfinance sends tz-aware `America/New_York`. `_last_bar_time` stripped the tz
    instead of converting it, so an 08:41 ET bar was stored as 08:41 UTC: four
    hours early. `_same_trading_day` then compares ET calendar dates, which
    under EDT still lands on the right day — correct by coincidence.
    """

    def _frame(self, index):
        return pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0],
             "Volume": [100]},
            index=pd.DatetimeIndex(index),
        )

    def test_a_yfinance_eastern_index_is_converted_not_stripped(self):
        df = self._frame(pd.to_datetime(["2026-09-28 08:41:00"]).tz_localize(
            "America/New_York"))
        assert mv._last_bar_time(df) == datetime(2026, 9, 28, 12, 41)

    def test_a_utc_index_is_unchanged(self):
        df = self._frame(pd.to_datetime(["2026-09-28 12:41:00"]).tz_localize("UTC"))
        assert mv._last_bar_time(df) == datetime(2026, 9, 28, 12, 41)

    def test_an_already_naive_index_passes_through(self):
        df = self._frame(pd.to_datetime(["2026-09-28 12:41:00"]))
        assert mv._last_bar_time(df) == datetime(2026, 9, 28, 12, 41)

    def test_winter_is_the_case_that_actually_broke(self):
        """EST is UTC-5, so a 04:00-04:59 ET pre-market bar read as UTC lands on
        the PREVIOUS ET date, and the freshness gate suppresses a live candidate
        as `stale_session_data`. Converting keeps it on the right day."""
        from src.scanner.movers_alerts import _same_trading_day
        df = self._frame(pd.to_datetime(["2027-01-11 04:30:00"]).tz_localize(
            "America/New_York"))
        bars_as_of = mv._last_bar_time(df)
        now = datetime(2027, 1, 11, 13, 0)          # 08:00 EST, naive UTC
        assert _same_trading_day(bars_as_of, now) is True
        # What the strip produced, for contrast: the wall clock, tz discarded.
        stripped = df.index[-1].to_pydatetime().replace(tzinfo=None)
        assert _same_trading_day(stripped, now) is False

    def test_unreadable_input_is_none_not_a_raise(self):
        assert mv._last_bar_time(pd.DataFrame()) is None
        assert mv._last_bar_time(None) is None
