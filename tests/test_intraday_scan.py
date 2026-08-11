"""Tests for the P1.2 continuous intraday scanner."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

import src.utils.config as cfg
from src.scanner import intraday_scan as isc
from src.scanner.candidate_queue import CandidateQueue

NOW = datetime(2026, 1, 2, 20, 30, 0)


@pytest.fixture(autouse=True)
def _scan_config(monkeypatch):
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_MIN_BARS", 10)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_REL_VOLUME_THRESHOLD", 2.0)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_MOMENTUM_PCT_THRESHOLD", 1.5)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_MOMENTUM_LOOKBACK_BARS", 6)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_BREAKOUT_LOOKBACK_BARS", 20)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_INTERVAL", "5m")


def _frame(closes, highs=None, vols=None):
    n = len(closes)
    idx = pd.date_range("2026-01-02 15:00", periods=n, freq="5min")
    highs = highs or [c + 0.2 for c in closes]
    vols = vols or [1000] * n
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": closes, "Close": closes, "Volume": vols},
        index=idx,
    )


# ── score_intraday_frame ──────────────────────────────────────────────────────

def test_too_few_bars_returns_none():
    assert isc.score_intraday_frame(_frame([100] * 5)) is None
    assert isc.score_intraday_frame(pd.DataFrame()) is None


def test_relative_volume_excludes_current_bar():
    df = _frame([100] * 15, vols=[1000] * 14 + [5000])
    m = isc.score_intraday_frame(df)
    assert m["rel_volume"] == 5.0  # 5000 / mean(1000)


def test_momentum_over_lookback():
    closes = [100 + i * 0.5 for i in range(15)]  # +0.5/bar
    m = isc.score_intraday_frame(_frame(closes))
    # last=107.0, ref 6 bars back = closes[8]=104.0 → (107-104)/104*100
    assert m["momentum_pct"] == pytest.approx(2.88, abs=0.01)


def test_breakout_flag():
    up = _frame([100 + i for i in range(15)])          # new high on last bar
    flat = _frame([100] * 15)                            # not above prior highs
    assert isc.score_intraday_frame(up)["is_breakout"] is True
    assert isc.score_intraday_frame(flat)["is_breakout"] is False


# ── scan_intraday ─────────────────────────────────────────────────────────────

def test_scan_emits_volume_momentum_breakout():
    df = _frame([100 + i * 0.5 for i in range(15)], vols=[1000] * 14 + [5000])
    cands = isc.scan_intraday(["AAA"], now=NOW, fetch=lambda t, interval=None: df)
    kinds = {c.kind for c in cands}
    assert kinds == {"volume", "momentum", "breakout"}


def test_breakout_requires_volume_participation():
    # New high but ordinary volume → breakout suppressed (fakeout guard),
    # and momentum is small, so nothing fires.
    closes = [100] * 13 + [100.2, 100.3]  # tiny drift, last is a marginal new high
    df = _frame(closes, vols=[1000] * 15)
    cands = isc.scan_intraday(["AAA"], now=NOW, fetch=lambda t, interval=None: df)
    assert all(c.kind != "breakout" for c in cands)


def test_quiet_ticker_emits_nothing():
    df = _frame([100] * 15, vols=[1000] * 15)  # flat price, flat volume
    assert isc.scan_intraday(["AAA"], now=NOW, fetch=lambda t, interval=None: df) == []


def test_scan_isolates_per_ticker_errors():
    def flaky(ticker, interval=None):
        if ticker == "BAD":
            raise RuntimeError("boom")
        return _frame([100 + i * 0.5 for i in range(15)], vols=[1000] * 14 + [5000])

    cands = isc.scan_intraday(["BAD", "AAA"], now=NOW, fetch=flaky)
    assert {c.ticker for c in cands} == {"AAA"}  # BAD skipped, AAA still scanned


# ── ContinuousScanner ─────────────────────────────────────────────────────────

def test_scanner_run_once_enqueues_and_dedupes(monkeypatch):
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_UNIVERSE_CAP", 100)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_QUEUE_MAX", 100)
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_DEDUP_TTL_SEC", 300)
    df = _frame([100 + i * 0.5 for i in range(15)], vols=[1000] * 14 + [5000])
    sc = isc.ContinuousScanner(["AAA"], fetch=lambda t, interval=None: df)
    first = sc.run_once(now=NOW)
    second = sc.run_once(now=NOW)  # same cycle → all deduped
    assert first == 3 and second == 0
    assert len(sc.queue) == 3


def test_scanner_caps_universe(monkeypatch):
    monkeypatch.setattr(cfg, "CONTINUOUS_SCAN_UNIVERSE_CAP", 2)
    sc = isc.ContinuousScanner(["A", "B", "C", "D"])
    assert sc.tickers == ["A", "B"]


def test_scanner_interval_floored(monkeypatch):
    sc = isc.ContinuousScanner(["AAA"], interval_sec=1)
    assert sc.interval_sec == isc._MIN_INTERVAL_SEC  # floored up


def test_market_hours_gate():
    assert isc.is_market_hours(datetime(2026, 1, 3, 12, 0)) is False  # Saturday
    assert isc.is_market_hours(datetime(2026, 1, 2, 10, 0)) is True   # Friday 10:00 ET
    assert isc.is_market_hours(datetime(2026, 1, 2, 8, 0)) is False   # pre-open
