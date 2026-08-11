"""Tests for the P1.2 candidate queue (dedup, bounded, thread-safe)."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.scanner.candidate_queue import Candidate, CandidateQueue

T0 = datetime(2026, 1, 2, 15, 0, 0)


def _c(ticker="AAA", kind="momentum", metric=1.0):
    return Candidate(ticker, kind, metric, T0.isoformat())


def test_push_and_drain_fifo():
    q = CandidateQueue()
    assert q.push(_c("AAA"), now=T0) is True
    assert q.push(_c("BBB"), now=T0) is True
    drained = q.drain()
    assert [c.ticker for c in drained] == ["AAA", "BBB"]
    assert len(q) == 0  # drain empties


def test_dedup_within_ttl():
    q = CandidateQueue(dedup_ttl_sec=300)
    assert q.push(_c("AAA", "volume"), now=T0) is True
    assert q.push(_c("AAA", "volume"), now=T0 + timedelta(seconds=100)) is False  # still fresh
    assert q.push(_c("AAA", "volume"), now=T0 + timedelta(seconds=301)) is True   # window elapsed
    # A different kind for the same ticker is not a duplicate.
    assert q.push(_c("AAA", "momentum"), now=T0) is True


def test_dedup_disabled_with_zero_ttl():
    q = CandidateQueue(dedup_ttl_sec=0)
    assert q.push(_c("AAA"), now=T0) is True
    assert q.push(_c("AAA"), now=T0) is True  # no suppression


def test_bounded_drops_oldest():
    q = CandidateQueue(maxlen=2, dedup_ttl_sec=0)
    q.push(_c("AAA"), now=T0)
    q.push(_c("BBB"), now=T0)
    q.push(_c("CCC"), now=T0)  # evicts AAA
    assert [c.ticker for c in q.snapshot()] == ["BBB", "CCC"]


def test_snapshot_does_not_remove():
    q = CandidateQueue()
    q.push(_c("AAA"), now=T0)
    assert len(q.snapshot()) == 1
    assert len(q) == 1  # snapshot is non-destructive


def test_candidate_to_dict_and_key():
    c = _c("aaa", "breakout", 2.5)
    assert c.key() == ("AAA", "breakout")
    d = c.to_dict()
    assert d["ticker"] == "aaa" and d["kind"] == "breakout" and d["metric"] == 2.5
