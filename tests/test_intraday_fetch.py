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

def test_ttl_is_bar_scaled_for_intraday():
    assert fetcher._ttl_for_interval("1m") == 60
    assert fetcher._ttl_for_interval("5m") == 300
    assert fetcher._ttl_for_interval("15m") == 900


def test_ttl_is_daily_default_for_daily():
    assert fetcher._ttl_for_interval("1d") == cache.DEFAULT_TTL
    assert fetcher._ttl_for_interval("1wk") == cache.DEFAULT_TTL


def test_ttl_respects_floor(monkeypatch):
    monkeypatch.setattr(fetcher.config, "INTRADAY_CACHE_TTL_FLOOR_SEC", 120)
    assert fetcher._ttl_for_interval("1m") == 120  # floored up from 60
    assert fetcher._ttl_for_interval("5m") == 300  # already above floor


def test_fetch_ohlcv_caches_intraday_with_short_ttl(monkeypatch):
    captured = {}

    def fake_set(key, data, ttl=cache.DEFAULT_TTL):
        captured["ttl"] = ttl

    monkeypatch.setattr(fetcher.cache, "get", lambda k: None)
    monkeypatch.setattr(fetcher.cache, "set", fake_set)
    monkeypatch.setattr(fetcher, "retry_call", lambda *a, **k: _frame(["2026-01-02 15:00"]))
    fetcher.fetch_ohlcv("AAPL", period="5d", interval="5m")
    assert captured["ttl"] == 300


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
