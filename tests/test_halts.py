"""Tests for trade-halt detection (src/data/halts.py) and its movers wiring.

Hermetic: the feed HTTP boundary and the halt map are patched, so no network
and no key are needed. Fixture XML mirrors the Nasdaq UTP feed's shape,
including its namespaced tags.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.data import halts
from src.data.halts import HaltRecord, active_halts, parse_halt_feed
from src.scanner import movers as mv
from src.scanner.movers_alerts import MoversAlerter
from src.utils import config as cfg

_ET = ZoneInfo("America/New_York")

# Namespaced, like the real feed. WFF is open (no resumption); RESUMED has one
# in the past; LATER has one in the future.
_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ndaq="http://www.nasdaqtrader.com/" version="2.0"><channel>
  <item>
    <ndaq:IssueSymbol>WFF</ndaq:IssueSymbol>
    <ndaq:IssueName>WF Holding Limited</ndaq:IssueName>
    <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
    <ndaq:HaltDate>08/17/2026</ndaq:HaltDate>
    <ndaq:HaltTime>10:31:04</ndaq:HaltTime>
    <ndaq:ResumptionDate></ndaq:ResumptionDate>
    <ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>
    <ndaq:Market>NASDAQ</ndaq:Market>
  </item>
  <item>
    <ndaq:IssueSymbol>RESUMED</ndaq:IssueSymbol>
    <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
    <ndaq:HaltDate>08/17/2026</ndaq:HaltDate>
    <ndaq:HaltTime>09:45:00</ndaq:HaltTime>
    <ndaq:ResumptionDate>08/17/2026</ndaq:ResumptionDate>
    <ndaq:ResumptionTradeTime>09:50:00</ndaq:ResumptionTradeTime>
  </item>
  <item>
    <ndaq:IssueSymbol>LATER</ndaq:IssueSymbol>
    <ndaq:ReasonCode>T1</ndaq:ReasonCode>
    <ndaq:HaltDate>08/17/2026</ndaq:HaltDate>
    <ndaq:HaltTime>10:00:00</ndaq:HaltTime>
    <ndaq:ResumptionDate>08/17/2026</ndaq:ResumptionDate>
    <ndaq:ResumptionTradeTime>11:30:00</ndaq:ResumptionTradeTime>
  </item>
</channel></rss>"""

_NOW = datetime(2026, 8, 17, 10, 55, tzinfo=_ET)   # WFF halted, RESUMED back, LATER still out


# ── feed parsing ─────────────────────────────────────────────────────────────

class TestParsing:
    def test_parses_namespaced_fields_by_local_name(self):
        records = {r.symbol: r for r in parse_halt_feed(_FEED)}
        assert set(records) == {"WFF", "RESUMED", "LATER"}
        wff = records["WFF"]
        assert wff.reason_code == "LUDP"
        assert wff.halted_at == datetime(2026, 8, 17, 10, 31, 4, tzinfo=_ET)
        assert wff.resumes_at is None          # open halt
        assert wff.name == "WF Holding Limited"

    def test_reason_code_maps_to_readable_text(self):
        assert HaltRecord("X", reason_code="LUDP").reason == "LULD volatility pause"
        assert HaltRecord("X", reason_code="T1").reason == "news pending"
        # Unknown codes pass through rather than being dropped or mislabelled.
        assert HaltRecord("X", reason_code="ZZ9").reason == "ZZ9"
        assert HaltRecord("X").reason == "halted"

    @pytest.mark.parametrize("payload", ["", "not xml at all", "<rss><channel/></rss>"])
    def test_unparseable_feed_yields_no_records(self, payload):
        assert parse_halt_feed(payload) == []

    def test_items_without_a_symbol_are_skipped(self):
        xml = ('<rss><channel><item><ReasonCode>LUDP</ReasonCode></item>'
               '<item><IssueSymbol>OK</IssueSymbol></item></channel></rss>')
        assert [r.symbol for r in parse_halt_feed(xml)] == ["OK"]


# ── active-halt window ───────────────────────────────────────────────────────

class TestActiveWindow:
    def test_open_halt_is_active_and_resumed_one_is_not(self):
        records = {r.symbol: r for r in parse_halt_feed(_FEED)}
        assert records["WFF"].is_active(_NOW) is True        # no resumption yet
        assert records["RESUMED"].is_active(_NOW) is False   # resumed at 09:50
        assert records["LATER"].is_active(_NOW) is True      # resumes at 11:30

    def test_a_halt_in_the_future_is_not_yet_active(self):
        record = HaltRecord("X", halted_at=datetime(2026, 8, 17, 15, 0, tzinfo=_ET))
        assert record.is_active(_NOW) is False


# ── active_halts(): config gate + fail-open ──────────────────────────────────

def _with_feed(monkeypatch, text=_FEED, boom=False):
    """Patch the HTTP boundary and bypass the disk cache."""
    monkeypatch.setattr(cfg, "MOVERS_HALTS_ENABLED", True)
    monkeypatch.setattr(halts, "_retry_after", 0.0)   # clear any prior backoff
    monkeypatch.setattr(halts.cache, "get", lambda key: None)
    monkeypatch.setattr(halts.cache, "set", lambda *a, **k: None)

    def _get(url, **kwargs):
        if boom:
            raise RuntimeError("feed down")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.text = text
        return resp

    session = MagicMock()
    session.get.side_effect = _get
    monkeypatch.setattr(halts, "get_retry_session", lambda: session)


class TestActiveHalts:
    def test_returns_only_currently_halted_symbols(self, monkeypatch):
        _with_feed(monkeypatch)
        out = active_halts(now=_NOW)
        assert set(out) == {"WFF", "LATER"}
        assert out["WFF"].reason == "LULD volatility pause"

    def test_disabled_returns_empty_without_fetching(self, monkeypatch):
        _with_feed(monkeypatch)
        monkeypatch.setattr(cfg, "MOVERS_HALTS_ENABLED", False)
        assert active_halts(now=_NOW) == {}

    def test_feed_failure_fails_open_and_is_reported(self, monkeypatch):
        # Fail-open: an outage must never silently mute every alert, but the
        # caller can tell "nothing halted" from "could not check".
        _with_feed(monkeypatch, boom=True)
        assert active_halts(now=_NOW) == {}
        assert halts.last_fetch_ok() is False

    def test_successful_fetch_reports_ok(self, monkeypatch):
        _with_feed(monkeypatch)
        active_halts(now=_NOW)
        assert halts.last_fetch_ok() is True


# ── movers wiring ────────────────────────────────────────────────────────────

def _cand(ticker="WFF", **over):
    base = dict(ticker=ticker, price=3.19, change_pct=121.6, direction="long",
                sources=["gainers"], score=77.0, base_score=77.0, enriched=True)
    base.update(over)
    return mv.MoverCandidate(**base)


class TestMoversAnnotation:
    def _patch_halts(self, monkeypatch, mapping):
        monkeypatch.setattr("src.data.halts.active_halts", lambda *a, **k: mapping)

    def test_halted_candidate_is_flagged_not_dropped_by_default(self, monkeypatch):
        self._patch_halts(monkeypatch, {"WFF": HaltRecord("WFF", reason_code="LUDP")})
        monkeypatch.setattr(cfg, "MOVERS_HALT_EXCLUDE_FROM_LIST", False)
        out = mv.annotate_halts([_cand(), _cand("SAFE")])
        assert [c.ticker for c in out] == ["WFF", "SAFE"]     # kept
        assert out[0].is_halted is True
        assert out[0].halt_reason == "LULD volatility pause"
        assert out[1].is_halted is False

    def test_exclude_from_list_drops_halted_candidates(self, monkeypatch):
        self._patch_halts(monkeypatch, {"WFF": HaltRecord("WFF", reason_code="LUDP")})
        monkeypatch.setattr(cfg, "MOVERS_HALT_EXCLUDE_FROM_LIST", True)
        out = mv.annotate_halts([_cand(), _cand("SAFE")])
        assert [c.ticker for c in out] == ["SAFE"]

    def test_halt_lookup_failure_leaves_candidates_untouched(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("feed down")
        monkeypatch.setattr("src.data.halts.active_halts", boom)
        out = mv.annotate_halts([_cand()])
        assert len(out) == 1 and out[0].is_halted is False

    def test_halt_state_is_serialized(self):
        c = _cand()
        c.is_halted, c.halt_reason = True, "LULD volatility pause"
        row = c.as_dict()
        assert row["is_halted"] is True
        assert row["halt_reason"] == "LULD volatility pause"


class TestAlertSuppression:
    def test_halted_candidate_is_never_alerted(self, monkeypatch):
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_ENABLED", True)
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_MIN_SCORE", 70)
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_REQUIRE_ENRICHED", True)
        monkeypatch.setattr(cfg, "MOVERS_HALT_SUPPRESS_ALERTS", True)
        halted = _cand()
        halted.is_halted = True
        # Same candidate, same qualifying score — only the halt differs.
        assert MoversAlerter().process([halted], send=False) == []
        assert len(MoversAlerter().process([_cand()], send=False)) == 1

    def test_suppression_can_be_turned_off(self, monkeypatch):
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_ENABLED", True)
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_MIN_SCORE", 70)
        monkeypatch.setattr(cfg, "MOVERS_ALERTS_REQUIRE_ENRICHED", True)
        monkeypatch.setattr(cfg, "MOVERS_HALT_SUPPRESS_ALERTS", False)
        halted = _cand()
        halted.is_halted = True
        assert len(MoversAlerter().process([halted], send=False)) == 1


class TestMonitorHoldsHaltedSetups:
    """The regression this feature exists for.

    While halted no trades print, so relative volume decays toward zero and
    invalidation rule 3 reports "relative volume faded" on a setup that was
    never given the chance to fade.
    """

    def _monitor_with(self, monkeypatch, halted_map, rel_volume):
        from src.scanner.movers_monitor import MoversMonitor

        monkeypatch.setattr(cfg, "MOVERS_HALT_SKIP_INVALIDATION", True)
        monkeypatch.setattr(cfg, "MOVERS_MONITOR_RVOL_FLOOR", 1.0)
        monkeypatch.setattr(cfg, "MOVERS_MONITOR_MIN_SCORE", 50)
        monkeypatch.setattr(cfg, "MOVERS_MONITOR_MOMENTUM_FLIP", -0.5)
        monkeypatch.setattr(cfg, "MOVERS_MONITOR_REQUIRE_VWAP_HOLD", True)
        monkeypatch.setattr("src.data.halts.active_halts", lambda *a, **k: halted_map)

        monitor = MoversMonitor()
        monitor.watch([_cand()])

        def _enrich(c):
            c.rel_volume, c.momentum_pct, c.vwap_pct = rel_volume, 1.0, 1.0
            return c

        return monitor, monitor.evaluate(enrich=_enrich, alert=None)

    def test_halted_setup_is_held_not_invalidated_on_collapsed_volume(self, monkeypatch):
        monitor, transitions = self._monitor_with(
            monkeypatch, {"WFF": HaltRecord("WFF", reason_code="LUDP")}, rel_volume=0.1)
        assert transitions == []                      # no fake "volume faded"
        assert len(monitor.active()) == 1             # still being watched
        assert monitor.active()[0].candidate.is_halted is True

    def test_same_collapse_invalidates_when_not_halted(self, monkeypatch):
        # Control: identical signals, no halt → the rule fires as designed.
        monitor, transitions = self._monitor_with(monkeypatch, {}, rel_volume=0.1)
        assert len(transitions) == 1
        assert "relative volume faded" in transitions[0]["reason"]

    def test_halt_flag_clears_when_the_symbol_resumes(self, monkeypatch):
        monitor, _ = self._monitor_with(
            monkeypatch, {"WFF": HaltRecord("WFF", reason_code="LUDP")}, rel_volume=2.0)
        assert monitor.active()[0].candidate.is_halted is True
        monkeypatch.setattr("src.data.halts.active_halts", lambda *a, **k: {})
        monitor.evaluate(enrich=lambda c: c, alert=None)
        assert monitor.active()[0].candidate.is_halted is False
