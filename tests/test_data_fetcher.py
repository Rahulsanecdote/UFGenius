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


def test_fetch_ohlcv_falls_back_to_stale_cache_on_fetch_error(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    stale = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 5],
            "High": [1, 2, 3, 4, 5],
            "Low": [1, 2, 3, 4, 5],
            "Close": [1, 2, 3, 4, 5],
            "Volume": [100, 100, 100, 100, 100],
        },
        index=idx,
    )

    monkeypatch.setattr(fetcher.cache, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetcher.cache, "get_stale", lambda *_args, **_kwargs: stale)
    monkeypatch.setattr(fetcher, "retry_call", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")))

    result = fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")

    assert result.equals(stale)


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


def test_fetch_ticker_info_falls_back_to_stale_cache_on_fetch_error(monkeypatch):
    stale = {"marketCap": 9_000_000_000, "longName": "Stale Corp"}

    monkeypatch.setattr(fetcher.cache, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetcher.cache, "get_stale", lambda *_args, **_kwargs: stale)
    monkeypatch.setattr(fetcher, "retry_call", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")))

    result = fetcher.fetch_ticker_info("AAPL")

    assert result == stale


def test_fetch_ticker_info_backfills_market_cap_from_fast_info(monkeypatch):
    class _FakeFastInfo:
        def get(self, key, default=None):
            values = {
                "market_cap": 2_500_000_000,
                "shares": 100_000_000,
                "last_price": 25.0,
                "currency": "USD",
                "exchange": "NYSE",
            }
            return values.get(key, default)

    class _FakeTicker:
        def __init__(self):
            self.fast_info = _FakeFastInfo()

        @property
        def info(self):
            return {"longName": "Advance Auto Parts"}

    monkeypatch.setattr(fetcher.yf, "Ticker", lambda _symbol: _FakeTicker())

    info = fetcher._fetch_ticker_info_once("AAP")

    assert info["longName"] == "Advance Auto Parts"
    assert info["marketCap"] == 2_500_000_000
    assert info["sharesOutstanding"] == 100_000_000
    assert info["currentPrice"] == 25.0
    assert info["currency"] == "USD"
    assert info["exchange"] == "NYSE"


def test_get_fundamentals_returns_empty_dict_when_provider_returns_none(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_ticker_info", lambda *_args, **_kwargs: None)

    result = fetcher.get_fundamentals("AAPL")

    assert result == {}


def test_get_fundamentals_returns_empty_dict_when_provider_returns_non_dict(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_ticker_info", lambda *_args, **_kwargs: "bad-payload")

    result = fetcher.get_fundamentals("AAPL")

    assert result == {}


def test_get_fundamentals_merges_dict_payload_safely(monkeypatch):
    provider_payload = {"marketCap": 2_000_000_000, "longName": "Acme Inc"}
    monkeypatch.setattr(fetcher, "fetch_ticker_info", lambda *_args, **_kwargs: provider_payload)

    result = fetcher.get_fundamentals("AAPL")

    assert result == provider_payload


def _mock_diagnose_price_history(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    sample = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13, 14],
            "High": [11, 12, 13, 14, 15],
            "Low": [9, 10, 11, 12, 13],
            "Close": [10, 11, 12, 13, 14],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        },
        index=idx,
    )

    class _FakeTicker:
        def __init__(self, _symbol):
            self.symbol = _symbol

        def history(self, **_kwargs):
            return sample

    monkeypatch.setattr(fetcher.yf, "Ticker", _FakeTicker)


def test_diagnose_marks_fundamentals_ok_when_get_fundamentals_returns_dict(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {"marketCap": 5_000_000_000})

    result = fetcher.diagnose()

    assert result["fundamentals"]["status"] == "OK"
    assert result["overall"] == "HEALTHY"


def test_diagnose_marks_fundamentals_empty_when_get_fundamentals_returns_empty_dict(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {})

    result = fetcher.diagnose()

    assert result["fundamentals"]["status"] == "EMPTY"
    assert result["overall"] == "DEGRADED"


def test_diagnose_handles_get_fundamentals_exception_as_error(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(fetcher, "get_fundamentals", _boom)

    result = fetcher.diagnose()

    assert result["fundamentals"]["status"] == "ERROR"
    assert "provider exploded" in result["fundamentals"]["error"]
    assert result["overall"] == "DEGRADED"


def test_get_critical_cache_freshness_flags_stale_symbol(monkeypatch):
    def _fake_meta(key, allow_expired=True):
        data = {
            "info:AAPL": {"age_sec": 1200, "is_expired": False},
            "ohlcv:SPY:1y:1d": {"age_sec": 7500, "is_expired": False},
            "ohlcv:^VIX:3mo:1d": {"age_sec": 900, "is_expired": False},
        }
        return data.get(key)

    monkeypatch.setattr(fetcher.cache, "get_metadata", _fake_meta)

    freshness = fetcher.get_critical_cache_freshness(max_age_sec=3600)

    assert freshness["any_critical_stale"] is True
    assert freshness["symbols"]["SPY"]["is_stale"] is True
    assert freshness["symbols"]["AAPL"]["is_stale"] is False
    assert freshness["max_age_human"] == "2h 05m"


def test_diagnose_includes_cache_freshness_payload(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {"marketCap": 5_000_000_000})
    monkeypatch.setattr(
        fetcher,
        "get_critical_cache_freshness",
        lambda *_args, **_kwargs: {
            "symbols": {},
            "stale_threshold_sec": 3600,
            "any_stale": True,
            "any_critical_stale": True,
            "max_age_sec": 7200,
            "max_age_human": "2h 00m",
        },
    )

    result = fetcher.diagnose()

    assert result["overall"] == "HEALTHY"
    assert result["cache_freshness"]["any_critical_stale"] is True
    assert result["cache_freshness"]["max_age_human"] == "2h 00m"
