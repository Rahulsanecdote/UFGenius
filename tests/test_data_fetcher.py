"""Tests for fetch retries/caching behavior."""

from __future__ import annotations

import pandas as pd

from src.data import fetcher


def test_fetch_ohlcv_uses_cache(monkeypatch):
    calls = {"n": 0}
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    sample = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 5],
            "High": [1, 2, 3, 4, 5],
            "Low": [1, 2, 3, 4, 5],
            "Close": [1, 2, 3, 4, 5],
            "Volume": [100, 100, 100, 100, 100],
        },
        index=idx,
    )

    def _fake_download(*_args, **_kwargs):
        calls["n"] += 1
        return sample

    fetcher.clear_data_caches()
    monkeypatch.setattr(fetcher, "_download_ohlcv_once", _fake_download)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

    one = fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")
    two = fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")
    assert calls["n"] == 1
    assert not one.empty and not two.empty


def test_fetch_ticker_info_uses_cache(monkeypatch):
    calls = {"n": 0}

    def _fake_once(*_args, **_kwargs):
        calls["n"] += 1
        return {"marketCap": 1_000}

    fetcher.clear_data_caches()
    monkeypatch.setattr(fetcher, "_fetch_ticker_info_once", _fake_once)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

    one = fetcher.fetch_ticker_info("AAPL")
    two = fetcher.fetch_ticker_info("AAPL")
    assert calls["n"] == 1
    assert one["marketCap"] == two["marketCap"] == 1_000
