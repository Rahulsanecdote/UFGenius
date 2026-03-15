"""Tests for fetch retries/caching behavior."""

from __future__ import annotations

import pandas as pd

from src.data import fetcher


class _FakeSemaphore:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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
    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_ticker_info_once", _fake_once)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

    one = fetcher.fetch_ticker_info("AAPL")
    two = fetcher.fetch_ticker_info("AAPL")
    assert calls["n"] == 1
    assert one["marketCap"] == two["marketCap"] == 1_000


def test_fetch_ticker_info_falls_back_to_stale_cache_on_fetch_error(monkeypatch):
    stale = {"marketCap": 9_000_000_000, "longName": "Stale Corp"}

    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: False)
    monkeypatch.setattr(fetcher.cache, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetcher.cache, "get_stale", lambda *_args, **_kwargs: stale)
    monkeypatch.setattr(
        fetcher,
        "_fetch_ticker_info_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

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


def test_download_ohlcv_once_prefers_alpaca_over_yfinance(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    alpaca_df = pd.DataFrame(
        {
            "Open": [10, 11, 12],
            "High": [11, 12, 13],
            "Low": [9, 10, 11],
            "Close": [10, 11, 12],
            "Volume": [1000, 1100, 1200],
        },
        index=idx,
    )

    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
    monkeypatch.setattr(fetcher, "_download_ohlcv_via_alpaca", lambda *_args, **_kwargs: alpaca_df)
    monkeypatch.setattr(
        fetcher,
        "_download_ohlcv_via_ticker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("yfinance ticker path should not run")),
    )

    result = fetcher._download_ohlcv_once("AAPL", period="1y", interval="1d")

    assert result.equals(alpaca_df)


def test_download_ohlcv_once_falls_back_to_yfinance_when_alpaca_fails(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    yf_df = pd.DataFrame(
        {
            "Open": [20, 21, 22],
            "High": [21, 22, 23],
            "Low": [19, 20, 21],
            "Close": [20, 21, 22],
            "Volume": [2000, 2100, 2200],
        },
        index=idx,
    )

    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
    monkeypatch.setattr(
        fetcher,
        "_download_ohlcv_via_alpaca",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("alpaca unavailable")),
    )
    monkeypatch.setattr(fetcher, "_download_ohlcv_via_ticker", lambda *_args, **_kwargs: yf_df)

    result = fetcher._download_ohlcv_once("AAPL", period="1y", interval="1d")

    assert result.equals(yf_df)


def test_download_ohlcv_once_falls_back_when_alpaca_payload_is_too_short(monkeypatch):
    short_idx = pd.date_range("2024-01-01", periods=4, freq="D")
    long_idx = pd.date_range("2024-01-01", periods=220, freq="D")
    short_df = pd.DataFrame(
        {
            "Open": [10, 11, 12, 13],
            "High": [11, 12, 13, 14],
            "Low": [9, 10, 11, 12],
            "Close": [10, 11, 12, 13],
            "Volume": [1000, 1100, 1200, 1300],
        },
        index=short_idx,
    )
    yf_df = pd.DataFrame(
        {
            "Open": list(range(220)),
            "High": list(range(1, 221)),
            "Low": list(range(220)),
            "Close": list(range(1, 221)),
            "Volume": [2000] * 220,
        },
        index=long_idx,
    )

    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
    monkeypatch.setattr(fetcher, "_download_ohlcv_via_alpaca", lambda *_args, **_kwargs: short_df)
    monkeypatch.setattr(fetcher, "_download_ohlcv_via_ticker", lambda *_args, **_kwargs: yf_df)

    result = fetcher._download_ohlcv_once("AAPL", period="1y", interval="1d")

    assert len(result) == 220
    assert result.equals(yf_df)


def test_fetch_ohlcv_ignores_short_cached_dataframe(monkeypatch):
    short_idx = pd.date_range("2024-01-01", periods=3, freq="D")
    long_idx = pd.date_range("2024-01-01", periods=220, freq="D")
    short_cached = pd.DataFrame(
        {
            "Open": [10, 11, 12],
            "High": [11, 12, 13],
            "Low": [9, 10, 11],
            "Close": [10, 11, 12],
            "Volume": [1000, 1100, 1200],
        },
        index=short_idx,
    )
    fresh = pd.DataFrame(
        {
            "Open": list(range(220)),
            "High": list(range(1, 221)),
            "Low": list(range(220)),
            "Close": list(range(1, 221)),
            "Volume": [3000] * 220,
        },
        index=long_idx,
    )

    monkeypatch.setattr(fetcher.cache, "get", lambda *_args, **_kwargs: short_cached)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fresh)
    monkeypatch.setattr(fetcher.cache, "set", lambda *_args, **_kwargs: None)

    result = fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")

    assert len(result) == 220
    assert result.equals(fresh)


def test_fetch_ticker_info_prefers_alpaca_primary(monkeypatch):
    fetcher.clear_data_caches()
    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
    monkeypatch.setattr(fetcher, "_fetch_ticker_info_via_alpaca_once", lambda *_args, **_kwargs: {"longName": "Alpaca Inc"})
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(
        fetcher,
        "_fetch_ticker_info_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("yfinance fallback should not run")),
    )

    result = fetcher.fetch_ticker_info("AAPL")

    assert result["longName"] == "Alpaca Inc"


def test_fetch_ticker_info_falls_back_to_yfinance_when_alpaca_fails(monkeypatch):
    calls = {"n": 0}

    def _fake_yf(*_args, **_kwargs):
        calls["n"] += 1
        return {"marketCap": 1_500_000_000}

    fetcher.clear_data_caches()
    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: True)
    monkeypatch.setattr(
        fetcher,
        "_fetch_ticker_info_via_alpaca_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("alpaca down")),
    )
    monkeypatch.setattr(fetcher, "_fetch_ticker_info_once", _fake_yf)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

    result = fetcher.fetch_ticker_info("AAPL")

    assert result["marketCap"] == 1_500_000_000
    assert calls["n"] == 1


def test_upstream_ohlcv_download_path_enters_global_provider_semaphore(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    sample = pd.DataFrame(
        {
            "Open": [1, 2, 3],
            "High": [1, 2, 3],
            "Low": [1, 2, 3],
            "Close": [1, 2, 3],
            "Volume": [10, 10, 10],
        },
        index=idx,
    )
    sem = _FakeSemaphore()
    monkeypatch.setattr(fetcher, "_UPSTREAM_FETCH_SEMAPHORE", sem)
    monkeypatch.setattr(fetcher.yf, "download", lambda **_kwargs: sample)

    result = fetcher._download_ohlcv_via_download("AAPL", period="1y", interval="1d")

    assert sem.entered == 1
    assert result.equals(sample)


def test_upstream_ticker_info_path_enters_global_provider_semaphore(monkeypatch):
    class _FakeFastInfo:
        def get(self, _key, default=None):
            return default

    class _FakeTicker:
        @property
        def info(self):
            return {"longName": "Acme Corp", "marketCap": 1_000_000_000}

        @property
        def fast_info(self):
            return _FakeFastInfo()

    sem = _FakeSemaphore()
    monkeypatch.setattr(fetcher, "_UPSTREAM_FETCH_SEMAPHORE", sem)
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda _symbol: _FakeTicker())

    result = fetcher._fetch_ticker_info_once("AAPL")

    assert sem.entered == 1
    assert result["longName"] == "Acme Corp"


def test_fetch_ticker_info_does_not_cache_empty_payload(monkeypatch):
    calls = {"fetch": 0, "cache_set": 0}

    def _empty_info(*_args, **_kwargs):
        calls["fetch"] += 1
        return {}

    fetcher.clear_data_caches()
    monkeypatch.setattr(fetcher, "_alpaca_credentials_configured", lambda: False)
    monkeypatch.setattr(fetcher.cache, "get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetcher.cache, "get_stale", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fetcher.cache, "set", lambda *_args, **_kwargs: calls.__setitem__("cache_set", calls["cache_set"] + 1))
    monkeypatch.setattr(fetcher, "_fetch_ticker_info_once", _empty_info)
    monkeypatch.setattr(fetcher, "retry_call", lambda fn, *a, **kw: fn(*a, **kw))

    one = fetcher.fetch_ticker_info("AAPL")
    two = fetcher.fetch_ticker_info("AAPL")

    assert one == {}
    assert two == {}
    assert calls["fetch"] == 2
    assert calls["cache_set"] == 0


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

    monkeypatch.setattr(
        fetcher,
        "_probe_ohlcv_live",
        lambda *_args, **_kwargs: (
            sample,
            {"status": "OK", "source": "mock", "reason": None, "provider_failures": []},
        ),
    )


def test_diagnose_marks_fundamentals_ok_when_get_fundamentals_returns_dict(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {"marketCap": 5_000_000_000})

    result = fetcher.diagnose()

    assert result["fundamentals"]["status"] == "OK"
    assert result["overall"] == "HEALTHY"


def test_diagnose_marks_fundamentals_empty_when_get_fundamentals_returns_empty_dict(monkeypatch):
    _mock_diagnose_price_history(monkeypatch)
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        fetcher,
        "_fetch_ticker_info_with_diagnostics",
        lambda *_args, **_kwargs: (
            {},
            {
                "status": "EMPTY",
                "source": None,
                "reason": "ALL_PROVIDERS_FAILED_OR_EMPTY",
                "provider_failures": [],
            },
        ),
    )

    result = fetcher.diagnose()

    assert result["fundamentals"]["status"] == "EMPTY"
    assert result["fundamentals"]["reason"] == "ALL_PROVIDERS_FAILED_OR_EMPTY"
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


def test_diagnose_reports_explicit_price_failure_reason(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "_probe_ohlcv_live",
        lambda *_args, **_kwargs: (
            pd.DataFrame(),
            {
                "status": "EMPTY",
                "source": None,
                "reason": "ALL_PROVIDERS_FAILED_OR_EMPTY",
                "provider_failures": [{"provider": "yfinance_ticker", "reason": "RATE_LIMITED"}],
            },
        ),
    )
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        fetcher,
        "_fetch_ticker_info_with_diagnostics",
        lambda *_args, **_kwargs: (
            {},
            {
                "status": "EMPTY",
                "source": None,
                "reason": "ALL_PROVIDERS_FAILED_OR_EMPTY",
                "provider_failures": [{"provider": "yfinance", "reason": "RATE_LIMITED"}],
            },
        ),
    )

    result = fetcher.diagnose()

    assert result["tests"]["AAPL"]["status"] == "EMPTY"
    assert result["tests"]["AAPL"]["reason"] == "ALL_PROVIDERS_FAILED_OR_EMPTY"
    assert result["tests"]["AAPL"]["provider_failures"][0]["reason"] == "RATE_LIMITED"


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


def test_fast_info_failure_warns_once_per_symbol(monkeypatch, caplog):
    class _FakeTicker:
        @property
        def info(self):
            return {"longName": "Acme"}

        @property
        def fast_info(self):
            raise RuntimeError("fast-info-down")

    monkeypatch.setattr(fetcher.yf, "Ticker", lambda _symbol: _FakeTicker())
    monkeypatch.setattr(fetcher, "_FAST_INFO_FAILURE_WARNED_SYMBOLS", set())
    caplog.set_level("DEBUG")

    fetcher._fetch_ticker_info_once("AAPL")
    fetcher._fetch_ticker_info_once("AAPL")

    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "AAPL: fast_info unavailable" in rec.message
    ]
    assert len(warnings) == 1
