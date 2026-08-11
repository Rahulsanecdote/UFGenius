"""Tests for the P1.3 intraday candidate consumer."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

import src.utils.config as cfg
from src.scanner.candidate_queue import Candidate, CandidateQueue
from src.scanner.intraday_consumer import IntradayConsumer

# Just after the last bar of the 20-bar test frames (09:30 → 11:05 @ 5m), so the
# frames read as fresh against the consumer's P1.1 staleness guard.
T0 = datetime(2026, 1, 2, 11, 7, 0)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(cfg, "INTRADAY_MIN_SESSION_BARS", 6)
    monkeypatch.setattr(cfg, "INTRADAY_OPENING_RANGE_MINUTES", 30)
    monkeypatch.setattr(cfg, "INTRADAY_MIN_REL_VOLUME", 1.5)
    monkeypatch.setattr(cfg, "INTRADAY_REQUIRE_ABOVE_VWAP", True)
    monkeypatch.setattr(cfg, "INTRADAY_ATR_PERIOD", 14)
    monkeypatch.setattr(cfg, "INTRADAY_CONSUMER_MAX_PER_CYCLE", 20)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_INTERVAL", "5m")
    monkeypatch.setattr(cfg, "INTRADAY_MAX_STALENESS_INTERVALS", 3)


def _breakout_df():
    bars = [(100.0 + i * 0.1, 1000) for i in range(6)]
    bars += [(100.6 + i * 0.05, 1000) for i in range(13)]
    bars += [(103.0, 12000)]
    idx = pd.date_range("2026-01-02 09:30", periods=len(bars), freq="5min")
    return pd.DataFrame(
        {"Open": [b[0] for b in bars], "High": [b[0] + 0.15 for b in bars],
         "Low": [b[0] - 0.15 for b in bars], "Close": [b[0] for b in bars],
         "Volume": [b[1] for b in bars]},
        index=idx,
    )


def _flat_df():
    idx = pd.date_range("2026-01-02 09:30", periods=20, freq="5min")
    return pd.DataFrame(
        {"Open": [100] * 20, "High": [100.1] * 20, "Low": [99.9] * 20,
         "Close": [100] * 20, "Volume": [1000] * 20},
        index=idx,
    )


def _cand(ticker):
    return Candidate(ticker, "breakout", 1.0, T0.isoformat())


def test_consumer_produces_plan_for_confirmed_entry():
    q = CandidateQueue(dedup_ttl_sec=0)
    q.push(_cand("AAA"), now=T0)
    sink_calls = []
    consumer = IntradayConsumer(
        q, sink=sink_calls.append,
        fetch=lambda t, interval=None: _breakout_df(), account_size=10_000,
    )
    plans = consumer.drain_once(now=T0)
    assert len(plans) == 1 and plans[0]["ticker"] == "AAA"
    assert plans[0]["source"] == "intraday"
    assert len(sink_calls) == 1  # sink received the plan


def test_consumer_skips_non_entry_candidates():
    q = CandidateQueue(dedup_ttl_sec=0)
    q.push(_cand("FLAT"), now=T0)
    consumer = IntradayConsumer(q, fetch=lambda t, interval=None: _flat_df())
    assert consumer.drain_once(now=T0) == []


def test_consumer_respects_per_cycle_cap(monkeypatch):
    monkeypatch.setattr(cfg, "INTRADAY_CONSUMER_MAX_PER_CYCLE", 2)
    q = CandidateQueue(dedup_ttl_sec=0)
    for t in ["A", "B", "C", "D"]:
        q.push(_cand(t), now=T0)
    consumer = IntradayConsumer(q, fetch=lambda t, interval=None: _flat_df())
    consumer.drain_once(now=T0)
    assert len(q) == 2  # only 2 drained this cycle; 2 remain queued


def test_consumer_coalesces_candidates_by_ticker():
    # A volume-confirmed breakout queues several kinds for one ticker; the
    # consumer must emit a single plan, not one per kind.
    q = CandidateQueue(dedup_ttl_sec=0)
    q.push(Candidate("AAA", "volume", 5.0, T0.isoformat()), now=T0)
    q.push(Candidate("AAA", "breakout", 1.0, T0.isoformat()), now=T0)
    q.push(Candidate("AAA", "momentum", 3.0, T0.isoformat()), now=T0)
    calls = []
    consumer = IntradayConsumer(
        q, sink=calls.append, fetch=lambda t, interval=None: _breakout_df(), account_size=10_000,
    )
    plans = consumer.drain_once(now=T0)
    assert len(plans) == 1 and len(calls) == 1  # one plan for AAA despite 3 candidates


def test_consumer_rejects_stale_frames():
    q = CandidateQueue(dedup_ttl_sec=0)
    q.push(_cand("AAA"), now=T0)
    consumer = IntradayConsumer(q, fetch=lambda t, interval=None: _breakout_df(), account_size=10_000)
    late = datetime(2026, 1, 2, 15, 0, 0)  # ~4h after the last 11:05 bar → stale
    assert consumer.drain_once(now=late) == []


def test_consumer_isolates_per_candidate_errors():
    q = CandidateQueue(dedup_ttl_sec=0)
    q.push(_cand("BAD"), now=T0)
    q.push(_cand("AAA"), now=T0)

    def flaky(ticker, interval=None):
        if ticker == "BAD":
            raise RuntimeError("boom")
        return _breakout_df()

    consumer = IntradayConsumer(q, fetch=flaky, account_size=10_000)
    plans = consumer.drain_once(now=T0)
    assert [p["ticker"] for p in plans] == ["AAA"]  # BAD isolated, AAA still processed
