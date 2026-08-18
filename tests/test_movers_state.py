"""Tests for the Phase 7 shared worker↔web state store (movers_state.py).

Uses a temp path so nothing touches the real data/ file. Covers the round-trip
(publish → load → snapshot), the live/stale verdict, the bounded recent rings,
and the fail-soft behaviour on a missing/corrupt file.
"""

from datetime import datetime, timedelta, timezone

from unittest.mock import patch

import src.utils.config as cfg
from src.scanner.movers import MoverCandidate
from src.scanner.movers_monitor import WatchState
from src.scanner.movers_state import MoversWorkerState


def _state(tmp_path):
    return MoversWorkerState(path=str(tmp_path / "movers_worker.json"))


def _watch(ticker="NBIS", score=88.0, direction="long"):
    c = MoverCandidate(ticker=ticker, price=12.0, change_pct=15.0, direction=direction,
                       sources=["gainers"], score=score, rel_volume=2.4,
                       momentum_pct=1.8, vwap_pct=0.9, is_breakout=True, enriched=True)
    return WatchState(candidate=c, entry_score=score)


def test_missing_file_is_unavailable(tmp_path):
    snap = _state(tmp_path).load().snapshot()
    assert snap["available"] is False
    assert snap["live"] is False
    assert "not running" in snap["reason"]


def test_publish_then_load_roundtrip(tmp_path):
    s = _state(tmp_path)
    s.publish(cycle=3, stats={"discoveries": 2, "alerts": 1, "invalidations": 0},
              watching=[_watch("AAA"), _watch("BBB", direction="short")],
              scan_window_open=True)
    snap = _state(tmp_path).load().snapshot()
    assert snap["available"] is True
    assert snap["cycle"] == 3
    assert snap["watching_count"] == 2
    tickers = {w["ticker"] for w in snap["watching"]}
    assert tickers == {"AAA", "BBB"}
    # compact view carries the live signals
    aaa = next(w for w in snap["watching"] if w["ticker"] == "AAA")
    assert aaa["direction"] == "long"
    assert aaa["rel_volume"] == 2.4


def test_live_when_fresh_stale_when_old(tmp_path):
    s = _state(tmp_path)
    s.publish(cycle=1, stats={}, watching=[], scan_window_open=True)
    with patch.object(cfg, "MOVERS_WORKER_STATE_STALE_SEC", 180.0):
        fresh = _state(tmp_path).load()
        assert fresh.snapshot()["live"] is True
        # 10 minutes later the same snapshot is stale (naive UTC, like the store)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        assert fresh.snapshot(now=future)["live"] is False


def test_recent_rings_are_bounded(tmp_path):
    s = _state(tmp_path)
    with patch.object(cfg, "MOVERS_WORKER_STATE_MAX_RECENT", 3):
        for i in range(6):
            s.record_alerts([{"ticker": f"T{i}", "direction": "long", "score": 80, "sent": True}])
        s.publish(cycle=1, stats={}, watching=[], scan_window_open=True)
    snap = _state(tmp_path).load().snapshot()
    assert len(snap["recent_alerts"]) == 3
    assert [a["ticker"] for a in snap["recent_alerts"]] == ["T3", "T4", "T5"]  # newest kept


def test_invalidations_recorded_with_timestamp(tmp_path):
    s = _state(tmp_path)
    s.record_invalidations([{"ticker": "ZZZ", "direction": "long", "reason": "lost VWAP"}])
    s.publish(cycle=2, stats={}, watching=[], scan_window_open=True)
    snap = _state(tmp_path).load().snapshot()
    assert snap["recent_invalidations"][0]["ticker"] == "ZZZ"
    assert snap["recent_invalidations"][0]["reason"] == "lost VWAP"
    assert "at" in snap["recent_invalidations"][0]


def test_stream_status_and_live_price_attached(tmp_path):
    # Phase 8: publish carries the stream status block and attaches a fresh live
    # price to the matching watch view.
    s = _state(tmp_path)
    s.publish(cycle=5, stats={}, watching=[_watch("NBIS")], scan_window_open=True,
              stream_status={"live": True, "feed": "iex", "subscribed_count": 1,
                             "priced_count": 1, "tick_count": 9},
              stream_prices={"NBIS": {"price": 12.34, "age_seconds": 0.5, "fresh": True}})
    snap = _state(tmp_path).load().snapshot()
    assert snap["streaming"]["live"] is True and snap["streaming"]["feed"] == "iex"
    nbis = snap["watching"][0]
    assert nbis["live_price"] == 12.34 and nbis["live_fresh"] is True


def test_no_stream_means_no_streaming_block(tmp_path):
    s = _state(tmp_path)
    s.publish(cycle=1, stats={}, watching=[_watch("AAA")], scan_window_open=True)
    snap = _state(tmp_path).load().snapshot()
    assert "streaming" not in snap
    assert "live_price" not in snap["watching"][0]


def test_corrupt_file_is_unavailable_not_fatal(tmp_path):
    p = tmp_path / "movers_worker.json"
    p.write_text("{not valid json", encoding="utf-8")
    snap = MoversWorkerState(path=str(p)).load().snapshot()
    assert snap["available"] is False


def test_publish_never_raises_on_bad_input(tmp_path):
    s = _state(tmp_path)
    # a watching item missing .candidate must be swallowed, not raised
    s.publish(cycle=1, stats={}, watching=[object()], scan_window_open=True)
    # nothing was written because the view build failed → still unavailable
    assert _state(tmp_path).load().snapshot()["available"] is False


def test_catalyst_alert_fields_survive_the_ring(tmp_path):
    """A catalyst alert must stay distinguishable from a price-derived one.

    Both go through the same ring; only the catalyst alert carries a news tier
    and headline, and without them the dashboard cannot tell the two apart or
    show the receipt the alert is based on.
    """
    s = _state(tmp_path)
    s.record_alerts([
        {"ticker": "ACME", "direction": "long", "score": None, "sent": False,
         "tier": "strong", "headline": "FDA approves ACME's lead drug"},
        {"ticker": "BCME", "direction": "long", "score": 88.0, "sent": True},
    ])
    s.publish(cycle=1, stats={}, watching=[], scan_window_open=True)
    alerts = _state(tmp_path).load().snapshot()["recent_alerts"]

    catalyst, mover = alerts
    assert catalyst["tier"] == "strong"
    assert catalyst["headline"] == "FDA approves ACME's lead drug"
    assert catalyst["sent"] is False
    # A movers alert's row is unchanged — the new keys are absent, not null,
    # so `filter(a => a.tier)` cleanly separates the two kinds.
    assert "tier" not in mover and "headline" not in mover


def test_headline_is_bounded(tmp_path):
    # Wire headlines are arbitrary-length text from outside, and this snapshot
    # is rewritten every cycle.
    s = _state(tmp_path)
    s.record_alerts([{"ticker": "ACME", "tier": "strong", "headline": "x" * 5000}])
    s.publish(cycle=1, stats={}, watching=[], scan_window_open=True)
    assert len(_state(tmp_path).load().snapshot()["recent_alerts"][0]["headline"]) == 200


def test_features_flag_distinguishes_off_from_quiet(tmp_path):
    """`0 catalyst alerts` is ambiguous without a flag saying the layer is on."""
    s = _state(tmp_path)
    s.publish(cycle=1, stats={"catalyst_alerts": 0}, watching=[],
              scan_window_open=True, features={"catalyst_alerts": True})
    assert _state(tmp_path).load().snapshot()["features"] == {"catalyst_alerts": True}


def test_features_default_to_empty(tmp_path):
    s = _state(tmp_path)
    s.publish(cycle=1, stats={}, watching=[], scan_window_open=True)
    assert _state(tmp_path).load().snapshot()["features"] == {}
