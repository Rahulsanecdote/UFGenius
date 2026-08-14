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
