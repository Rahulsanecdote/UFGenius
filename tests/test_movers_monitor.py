"""Tests for Phase 4 movers monitoring + invalidation (movers_monitor.py).

Hermetic: the intraday enricher and Telegram sender are injected/mocked.
"""

from unittest.mock import MagicMock, patch

import src.utils.config as cfg
from src.scanner.movers import MoverCandidate
from src.scanner import movers_monitor as mm


def _thresholds(**over):
    base = dict(MOVERS_MONITOR_MOMENTUM_FLIP=-0.5, MOVERS_MONITOR_REQUIRE_VWAP_HOLD=True,
                MOVERS_MONITOR_RVOL_FLOOR=1.0, MOVERS_MONITOR_MIN_SCORE=50.0,
                MOVERS_MONITOR_ALERT_ON_INVALIDATION=True)
    base.update(over)
    return [patch.object(cfg, k, v) for k, v in base.items()]


def _with_thresholds(fn, **over):
    ps = _thresholds(**over)
    for p in ps:
        p.start()
    try:
        return fn()
    finally:
        for p in ps:
            p.stop()


def _cand(**over):
    d = dict(ticker="NBIS", price=259.2, change_pct=34.1, direction="long",
             sources=["gainers"], score=89.0, base_score=93.0, rel_volume=10.3,
             momentum_pct=3.7, vwap_pct=6.4, is_breakout=True, enriched=True)
    d.update(over)
    return MoverCandidate(**d)


# ── check_invalidation rules ──────────────────────────────────────────────────

def test_valid_setup_not_invalidated():
    inval, _ = _with_thresholds(lambda: mm.check_invalidation("long", 10.3, 3.7, 6.4, 89.0))
    assert inval is False


def test_momentum_flip_invalidates():
    inval, reason = _with_thresholds(lambda: mm.check_invalidation("long", 5.0, -2.0, 3.0, 80.0))
    assert inval and "momentum" in reason


def test_lost_vwap_invalidates_long():
    inval, reason = _with_thresholds(lambda: mm.check_invalidation("long", 5.0, 1.0, -1.5, 80.0))
    assert inval and "VWAP" in reason


def test_lost_vwap_invalidates_short_direction_aware():
    # For a SHORT, being ABOVE vwap (+) is the wrong side.
    inval, reason = _with_thresholds(lambda: mm.check_invalidation("short", 5.0, -1.0, 2.0, 80.0))
    assert inval and "VWAP" in reason
    # ...and below VWAP is fine for a short.
    ok, _ = _with_thresholds(lambda: mm.check_invalidation("short", 5.0, -1.0, -2.0, 80.0))
    assert ok is False


def test_volume_fade_invalidates():
    inval, reason = _with_thresholds(lambda: mm.check_invalidation("long", 0.6, 3.0, 3.0, 80.0))
    assert inval and "volume" in reason


def test_score_collapse_invalidates():
    inval, reason = _with_thresholds(lambda: mm.check_invalidation("long", 5.0, 3.0, 3.0, 40.0))
    assert inval and "score" in reason


# ── MoversMonitor lifecycle ───────────────────────────────────────────────────

def test_watch_adds_unique_candidates():
    monitor = mm.MoversMonitor()
    assert monitor.watch([_cand(), _cand()]) == 1          # same key → one
    assert monitor.watch([_cand(ticker="AMD")]) == 1
    assert len(monitor.active()) == 2


def test_evaluate_transitions_to_invalidated_when_move_dies():
    monitor = mm.MoversMonitor()
    monitor.watch([_cand()])

    # Injected enricher simulates the move fading on the next look.
    def fade(c):
        c.rel_volume, c.momentum_pct, c.vwap_pct, c.score = 0.5, -3.0, -2.0, 30.0

    trans = _with_thresholds(lambda: monitor.evaluate(enrich=fade))
    assert len(trans) == 1
    assert trans[0]["ticker"] == "NBIS" and trans[0]["status"] == "invalidated"
    assert monitor.active() == []                           # no longer watching


def test_evaluate_keeps_watching_when_still_valid():
    monitor = mm.MoversMonitor()
    monitor.watch([_cand()])

    def strong(c):  # still a healthy move
        c.rel_volume, c.momentum_pct, c.vwap_pct, c.score = 8.0, 4.0, 5.0, 85.0

    trans = _with_thresholds(lambda: monitor.evaluate(enrich=strong))
    assert trans == []
    assert len(monitor.active()) == 1
    assert monitor.active()[0].updates == 1


def test_invalidation_fires_alert_callback():
    monitor = mm.MoversMonitor()
    monitor.watch([_cand()])
    alert = MagicMock()

    def fade(c):
        c.rel_volume, c.momentum_pct, c.vwap_pct, c.score = 0.5, -3.0, -2.0, 30.0

    _with_thresholds(lambda: monitor.evaluate(enrich=fade, alert=alert))
    alert.assert_called_once()
    assert "INVALIDATED" in alert.call_args.args[0] and "NBIS" in alert.call_args.args[0]


def test_enrich_error_does_not_break_evaluate():
    monitor = mm.MoversMonitor()
    monitor.watch([_cand()])

    def boom(c):
        raise RuntimeError("network down")

    trans = _with_thresholds(lambda: monitor.evaluate(enrich=boom))
    assert trans == []                     # no crash
    assert len(monitor.active()) == 1      # still watched (unchanged)
