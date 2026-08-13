"""Tests for Phase 3 movers alerts (src/scanner/movers_alerts.py).

Hermetic: the Telegram sender is mocked and alerting is toggled via config.
"""

from datetime import datetime
from unittest.mock import patch

import src.utils.config as cfg
from src.scanner.movers import MoverCandidate
from src.scanner import movers_alerts as ma


def _cand(ticker="NBIS", direction="long", score=89.0, enriched=True,
          change_pct=34.1, rel_volume=10.3, momentum_pct=3.7, vwap_pct=6.4,
          is_breakout=True, sources=("gainers", "most_actives")):
    return MoverCandidate(
        ticker=ticker, price=259.2, change_pct=change_pct, direction=direction,
        sources=list(sources), score=score, base_score=93.0, rel_volume=rel_volume,
        momentum_pct=momentum_pct, vwap_pct=vwap_pct, is_breakout=is_breakout,
        enriched=enriched,
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
            first = alerter.process([_cand()], now=now)
            second = alerter.process([_cand()], now=now)  # same ticker, same window
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
