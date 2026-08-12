"""Tests for the P1.1 intraday fetch layer (TTL + fetch_intraday entry point)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

import src.data.fetcher as fetcher
from src.data import cache


def _frame(times: list[str]) -> pd.DataFrame:
    idx = pd.to_datetime(times)
    n = len(times)
    return pd.DataFrame(
        {"Open": range(n), "High": range(n), "Low": range(n),
         "Close": range(n), "Volume": [10] * n},
        index=idx,
    )


# ── interval-aware TTL ────────────────────────────────────────────────────────

def test_ttl_boundary_aligned(monkeypatch):
    # Default path: expire at the earliest boundary+settle instant after now.
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", True)
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_SETTLE_SEC", 5.0)
    # 5m boundaries are multiples of 300: now=1000 is past 900+5, so next is 1200+5 → 205.
    assert fetcher._ttl_for_interval("5m", now=1000.0) == 205
    # exactly on a boundary → this boundary's settle instant (1200+5) → 5, NOT a full bar.
    assert fetcher._ttl_for_interval("5m", now=1200.0) == 5
    # 1s before a boundary → a tiny TTL (the bar is about to close): 1200+5-1199.
    assert fetcher._ttl_for_interval("5m", now=1199.0) == 6
    # 1m: now=1000 is past 960+5, so next is 1020+5 → 25.
    assert fetcher._ttl_for_interval("1m", now=1000.0) == 25


def test_ttl_cold_start_within_settle_window(monkeypatch):
    # Codex P1: a fetch inside the settle window right after a boundary must
    # expire at THIS boundary's settle instant, not be pushed a full bar out.
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", True)
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_SETTLE_SEC", 5.0)
    # now = 962 is 2s past the 960 boundary; settle instant 965 hasn't passed → ttl 3.
    assert fetcher._ttl_for_interval("1m", now=962.0) == 3


def test_ttl_session_phased_intervals_use_legacy(monkeypatch):
    # Codex P2: hourly / 90m bars are session-phased (open 09:30 ET; 90m doesn't
    # divide the hour), so epoch-modulo alignment would be wrong — they keep the
    # legacy bar-length TTL even with boundary-align on.
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", True)
    assert fetcher._ttl_for_interval("60m", now=1000.0) == 3600
    assert fetcher._ttl_for_interval("1h", now=1000.0) == 3600
    assert fetcher._ttl_for_interval("90m", now=1000.0) == 5400


def test_ttl_is_bar_scaled_for_intraday_legacy(monkeypatch):
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", False)
    assert fetcher._ttl_for_interval("1m") == 60
    assert fetcher._ttl_for_interval("5m") == 300
    assert fetcher._ttl_for_interval("15m") == 900


def test_ttl_is_daily_default_for_daily():
    # Daily/unknown intervals ignore alignment entirely (no bar cadence).
    assert fetcher._ttl_for_interval("1d") == cache.DEFAULT_TTL
    assert fetcher._ttl_for_interval("1wk") == cache.DEFAULT_TTL


def test_ttl_respects_floor_legacy(monkeypatch):
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", False)
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_TTL_FLOOR_SEC", 120)
    assert fetcher._ttl_for_interval("1m") == 120  # floored up from 60
    assert fetcher._ttl_for_interval("5m") == 300  # already above floor


def test_fetch_ohlcv_caches_intraday_with_short_ttl(monkeypatch):
    captured = {}

    def fake_set(key, data, ttl=cache.DEFAULT_TTL):
        captured["ttl"] = ttl

    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", False)
    monkeypatch.setattr(fetcher.cache, "get", lambda k: None)
    monkeypatch.setattr(fetcher.cache, "set", fake_set)
    monkeypatch.setattr(fetcher, "retry_call", lambda *a, **k: _frame(["2026-01-02 15:00"]))
    fetcher.fetch_ohlcv("AAPL", period="5d", interval="5m")
    assert captured["ttl"] == 300


def test_fetch_ohlcv_caches_intraday_boundary_aligned(monkeypatch):
    # End-to-end: the boundary-aligned TTL is well under a full bar and > 0.
    captured = {}
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_BOUNDARY_ALIGN", True)
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_SETTLE_SEC", 5.0)
    monkeypatch.setattr(fetcher.cache, "get", lambda k: None)
    monkeypatch.setattr(fetcher.cache, "set", lambda key, data, ttl=cache.DEFAULT_TTL: captured.__setitem__("ttl", ttl))
    monkeypatch.setattr(fetcher, "retry_call", lambda *a, **k: _frame(["2026-01-02 15:00"]))
    fetcher.fetch_ohlcv("AAPL", period="5d", interval="5m")
    assert 0 < captured["ttl"] <= 305   # (settle, one 5m bar + settle]


# ── fetch_intraday ────────────────────────────────────────────────────────────

def test_fetch_intraday_rejects_daily_interval():
    with pytest.raises(ValueError, match="not an intraday interval"):
        fetcher.fetch_intraday("AAPL", interval="1d")


def test_fetch_intraday_sanitizes_output(monkeypatch):
    # Provider returns unordered bars, a duplicate, and a future-labelled bar.
    raw = _frame([
        "2026-01-02 15:05", "2026-01-02 15:00", "2026-01-02 15:05", "2026-01-02 16:00",
    ])
    monkeypatch.setattr(fetcher, "fetch_ohlcv", lambda *a, **k: raw)
    out = fetcher.fetch_intraday("AAPL", interval="5m", now=datetime(2026, 1, 2, 15, 30))
    assert list(out.index) == list(pd.to_datetime(["2026-01-02 15:00", "2026-01-02 15:05"]))


def test_fetch_intraday_uses_default_interval(monkeypatch):
    calls = {}

    def fake_fetch(ticker, period=None, interval=None, use_cache=True):
        calls["interval"] = interval
        calls["period"] = period
        return _frame(["2026-01-02 15:00"])

    monkeypatch.setattr(fetcher.config, "INTRADAY_DEFAULT_INTERVAL", "5m")
    monkeypatch.setattr(fetcher, "fetch_ohlcv", fake_fetch)
    fetcher.fetch_intraday("AAPL", now=datetime(2026, 1, 2, 15, 30))
    assert calls["interval"] == "5m"
    assert calls["period"] == "5d"  # per-interval default lookback


def test_fetch_intraday_empty_provider_returns_empty(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_ohlcv", lambda *a, **k: pd.DataFrame())
    assert fetcher.fetch_intraday("AAPL", interval="5m").empty
