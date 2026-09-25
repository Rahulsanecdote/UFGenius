"""Tests for Phase 3 movers alerts (src/scanner/movers_alerts.py).

Hermetic: the Telegram sender is mocked and alerting is toggled via config.
"""

from datetime import datetime
from unittest.mock import patch

import src.utils.config as cfg
from src.scanner.movers import MoverCandidate
from src.scanner import movers_alerts as ma

_UNSET = object()


def _cand(ticker="NBIS", direction="long", score=89.0, enriched=True,
          change_pct=34.1, rel_volume=10.3, momentum_pct=3.7, vwap_pct=6.4,
          is_breakout=True, sources=("gainers", "most_actives"),
          bars_as_of=_UNSET):
    # Default to bars from the current session: the freshness gate is a real
    # suppression reason now, so a candidate with no `bars_as_of` is held back
    # and every test about the OTHER rules would silently stop testing them.
    return MoverCandidate(
        ticker=ticker, price=259.2, change_pct=change_pct, direction=direction,
        sources=list(sources), score=score, base_score=93.0, rel_volume=rel_volume,
        momentum_pct=momentum_pct, vwap_pct=vwap_pct, is_breakout=is_breakout,
        enriched=enriched,
        bars_as_of=ma._utcnow() if bars_as_of is _UNSET else bars_as_of,
    )


def _alerts_on(**over):
    base = dict(MOVERS_ALERTS_ENABLED=True, MOVERS_ALERTS_MIN_SCORE=70.0,
                MOVERS_ALERTS_REQUIRE_ENRICHED=True, MOVERS_ALERTS_DEDUP_TTL_SEC=1800.0,
                MOVERS_ALERTS_MAX_PER_RUN=10)
    base.update(over)
    return [patch.object(cfg, k, v) for k, v in base.items()]


def _run(candidates, *, send=True, now=None, **over):
    ps = _alerts_on(**over)
    for p in ps:
        p.start()
    try:
        return ma.MoversAlerter().process(candidates, send=send, now=now)
    finally:
        for p in ps:
            p.stop()


# ── message content ───────────────────────────────────────────────────────────

def test_format_alert_contains_why_confidence_direction():
    msg = ma.format_alert(_cand())
    assert "🟢 LONG" in msg and "NBIS" in msg
    assert "Confidence: VERY HIGH (89/100)" in msg
    assert "relative volume 10.3x" in msg
    assert "above VWAP" in msg and "breakout" in msg
    assert "NOT financial advice" in msg


def test_short_setup_message():
    msg = ma.format_alert(_cand(ticker="BEAR", direction="short", change_pct=-12.0,
                                 momentum_pct=-4.0, vwap_pct=-3.0, is_breakout=False))
    assert "🔴 SHORT" in msg
    assert "below VWAP" in msg


# ── gating / dedup ────────────────────────────────────────────────────────────

def test_disabled_is_noop():
    with patch.object(cfg, "MOVERS_ALERTS_ENABLED", False):
        assert ma.MoversAlerter().process([_cand()]) == []


def test_below_min_score_not_alerted():
    fired = _run([_cand(score=60.0)], send=False)
    assert fired == []


def test_requires_enriched_when_configured():
    fired = _run([_cand(enriched=False)], send=False)
    assert fired == []
    fired2 = _run([_cand(enriched=False)], send=False, MOVERS_ALERTS_REQUIRE_ENRICHED=False)
    assert len(fired2) == 1


def test_dedup_suppresses_repeat_within_ttl():
    alerter = None
    ps = _alerts_on()
    for p in ps:
        p.start()
    try:
        with patch("src.scanner.movers_alerts.send_text_alert", return_value=True):
            alerter = ma.MoversAlerter()
            now = datetime(2026, 1, 2, 15, 0, 0)
            first = alerter.process([_cand(bars_as_of=now)], now=now)
            second = alerter.process([_cand(bars_as_of=now)], now=now)  # same ticker, same window
    finally:
        for p in ps:
            p.stop()
    assert len(first) == 1 and second == []


def test_max_per_run_caps_alerts():
    cands = [_cand(ticker=f"T{i}") for i in range(5)]
    fired = _run(cands, send=False, MOVERS_ALERTS_MAX_PER_RUN=2)
    assert len(fired) == 2


# ── sending ───────────────────────────────────────────────────────────────────

def test_send_uses_telegram_sender():
    with patch("src.scanner.movers_alerts.send_text_alert", return_value=True) as mock_send:
        fired = _run([_cand()])
    assert len(fired) == 1 and fired[0]["sent"] is True
    mock_send.assert_called_once()
    assert "NBIS" in mock_send.call_args.args[0]


def test_send_failure_recorded_but_not_raised():
    with patch("src.scanner.movers_alerts.send_text_alert", return_value=False):
        fired = _run([_cand()])
    assert len(fired) == 1 and fired[0]["sent"] is False


# ── held-back candidates are reported, not silently dropped ──────────────────

class TestSuppressionVisibility:
    """A qualifying name that does not alert must say why.

    Observed 2026-08-19: ZSTK ran +370%, scored 85, entered the watch set, and
    never alerted because its intraday bars were unavailable. The suppression
    was correct; being unable to tell it apart from "never discovered" was not.
    """

    def test_unenriched_candidate_is_reported_with_a_reason(self):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                fired = alerter.process([_cand("ZSTK", score=85.0, enriched=False,
                                               rel_volume=None)])
        finally:
            for p in ctx:
                p.stop()
        assert fired == []
        held = alerter.last_suppressed()
        assert [(h["ticker"], h["reason"]) for h in held] == [("ZSTK", "no_intraday_data")]
        assert held[0]["score"] == 85.0

    def test_halted_candidate_is_reported(self):
        ctx = _alerts_on(MOVERS_HALT_SUPPRESS_ALERTS=True)
        for p in ctx:
            p.start()
        try:
            c = _cand("WFF", score=80.0)
            c.is_halted = True
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                alerter.process([c])
        finally:
            for p in ctx:
                p.stop()
        assert alerter.last_suppressed()[0]["reason"] == "halted"

    def test_sub_threshold_candidates_are_not_reported(self):
        # The below-score majority is noise, not a withheld decision — surfacing
        # it would bury the one entry that means something.
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                alerter.process([_cand("LOW", score=12.0, enriched=False)])
        finally:
            for p in ctx:
                p.stop()
        assert alerter.last_suppressed() == []

    def test_dedup_is_recorded_distinctly_from_a_data_gap(self):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                assert len(alerter.process([_cand("AAA")])) == 1
                alerter.process([_cand("AAA")])          # inside the TTL
        finally:
            for p in ctx:
                p.stop()
        assert alerter.last_suppressed()[0]["reason"] == "already_alerted"

    def test_alerting_candidates_are_not_listed_as_held(self):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                fired = alerter.process([_cand("GOOD"), _cand("ZSTK", score=85.0,
                                                              enriched=False)])
        finally:
            for p in ctx:
                p.stop()
        assert [f["ticker"] for f in fired] == ["GOOD"]
        assert [h["ticker"] for h in alerter.last_suppressed()] == ["ZSTK"]

    def test_held_list_is_bounded(self):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                alerter.process([_cand(f"T{i}", score=80.0, enriched=False)
                                 for i in range(40)])
        finally:
            for p in ctx:
                p.stop()
        assert len(alerter.last_suppressed()) == ma._MAX_SUPPRESSED

    def test_each_run_replaces_the_previous_list(self):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                alerter.process([_cand("ZSTK", score=85.0, enriched=False)])
                alerter.process([_cand("OTHER", score=85.0, enriched=False)])
        finally:
            for p in ctx:
                p.stop()
        assert [h["ticker"] for h in alerter.last_suppressed()] == ["OTHER"]


# ── the freshness gate (movers.alerts.require_fresh_session) ─────────────────
# Before 09:30 the movers chain serves the PREVIOUS session and the intraday
# fetch returns yesterday's bars, so `require_enriched` passes on metrics that
# describe a finished day while the alert reads as live.


class TestRequireFreshSession:
    """The 2026-09-25 regression.

    Alerts fired at 08:18-08:19 ET carried Thursday's closing prices and
    Thursday's day moves verbatim: HUBC $2.33 (-27.4%), TRT $7.26 (-36.6%),
    AVX $5.45 (+32.9%) — each an exact match for the prior session's close and
    change, presented to the operator as a live intraday setup.
    """

    # 2026-09-25 08:18 ET, when the phantom alerts fired (12:18 UTC).
    FRIDAY_PREMARKET = datetime(2026, 9, 25, 12, 18)
    # The last bar the enrichment could reach: Thursday's 15:55 ET close.
    THURSDAY_CLOSE = datetime(2026, 9, 24, 19, 55)

    def _held(self, cand, now):
        ctx = _alerts_on()
        for p in ctx:
            p.start()
        try:
            alerter = ma.MoversAlerter()
            with patch.object(ma, "send_text_alert", return_value=True):
                fired = alerter.process([cand], now=now)
        finally:
            for p in ctx:
                p.stop()
        return fired, alerter.last_suppressed()

    def test_yesterdays_bars_do_not_alert(self):
        fired, held = self._held(
            _cand("HUBC", direction="short", change_pct=-27.4, score=80.0,
                  bars_as_of=self.THURSDAY_CLOSE),
            self.FRIDAY_PREMARKET,
        )
        assert fired == []
        assert [(h["ticker"], h["reason"]) for h in held] == [("HUBC", "stale_session_data")]

    def test_todays_bars_still_alert(self):
        """Control: the only difference is which session the bars came from."""
        fired, held = self._held(
            _cand("HUBC", direction="short", change_pct=-27.4, score=80.0,
                  bars_as_of=datetime(2026, 9, 25, 13, 45)),   # 09:45 ET, same day
            self.FRIDAY_PREMARKET,
        )
        assert len(fired) == 1 and held == []

    def test_enriched_alone_does_not_establish_freshness(self):
        """`enriched` is True in the failure case — there ARE bars, wrong day."""
        c = _cand("TRT", direction="short", change_pct=-36.6, score=72.0,
                  bars_as_of=self.THURSDAY_CLOSE)
        assert c.enriched is True
        fired, _ = self._held(c, self.FRIDAY_PREMARKET)
        assert fired == []

    def test_missing_timestamp_is_treated_as_stale(self):
        """Unestablished is not current — the gate may not pass on silence."""
        fired, held = self._held(
            _cand("AVX", change_pct=32.9, score=72.0, bars_as_of=None),
            self.FRIDAY_PREMARKET,
        )
        assert fired == []
        assert held[0]["reason"] == "stale_session_data"

    def test_weekend_gap_does_not_make_friday_bars_current(self):
        """Monday pre-market: the freshest bars are Friday's. ET date, not age."""
        monday_premarket = datetime(2026, 9, 28, 12, 18)     # 08:18 ET Monday
        fired, _ = self._held(
            _cand("AVX", change_pct=32.9, score=72.0,
                  bars_as_of=datetime(2026, 9, 25, 19, 55)),  # Friday 15:55 ET
            monday_premarket,
        )
        assert fired == []

    def test_utc_date_rollover_is_not_a_new_session(self):
        """15:30 ET is 19:30 UTC; 09:45 ET next day is 13:45 UTC — same UTC-day
        arithmetic would call these one session and the reverse pair two."""
        assert ma._same_trading_day(datetime(2026, 9, 24, 19, 30),
                                    datetime(2026, 9, 25, 13, 45)) is False
        # 20:30 ET Thursday is already 2026-09-25 in UTC, still Thursday in ET.
        assert ma._same_trading_day(datetime(2026, 9, 25, 0, 30),
                                    datetime(2026, 9, 24, 19, 30)) is True

    def test_gate_can_be_turned_off(self):
        fired, _ = self._held(
            _cand("HUBC", direction="short", change_pct=-27.4, score=80.0,
                  bars_as_of=self.THURSDAY_CLOSE),
            self.FRIDAY_PREMARKET,
        )
        assert fired == []
        ctx = _alerts_on(MOVERS_ALERTS_REQUIRE_FRESH_SESSION=False)
        for p in ctx:
            p.start()
        try:
            with patch.object(ma, "send_text_alert", return_value=True):
                fired = ma.MoversAlerter().process(
                    [_cand("HUBC", direction="short", score=80.0,
                           bars_as_of=self.THURSDAY_CLOSE)],
                    now=self.FRIDAY_PREMARKET)
        finally:
            for p in ctx:
                p.stop()
        assert len(fired) == 1
