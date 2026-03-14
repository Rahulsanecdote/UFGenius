"""Tests for src/data/universe.py — universe fetching, fallback, and filtering."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.universe import (
    _fallback_sp500,
    filter_universe,
    get_universe,
)


# ── get_universe routing ──────────────────────────────────────────────────────

def test_get_universe_sp500_calls_sp500_fetcher():
    with patch("src.data.universe.get_sp500_tickers", return_value=["AAPL", "MSFT"]) as mock_sp:
        result = get_universe("SP500")
    mock_sp.assert_called_once()
    assert result == ["AAPL", "MSFT"]


def test_get_universe_russell1000_calls_russell_fetcher():
    with patch("src.data.universe.get_russell1000_tickers", return_value=["AAPL"]) as mock_r:
        result = get_universe("RUSSELL1000")
    mock_r.assert_called_once()
    assert result == ["AAPL"]


def test_get_universe_unknown_falls_back_to_sp500():
    with patch("src.data.universe.get_sp500_tickers", return_value=["AAPL"]) as mock_sp:
        result = get_universe("NONEXISTENT")
    mock_sp.assert_called_once()
    assert result == ["AAPL"]


# ── Fallback list ─────────────────────────────────────────────────────────────

def test_fallback_sp500_returns_non_empty_list():
    tickers = _fallback_sp500()
    assert isinstance(tickers, list)
    assert len(tickers) >= 10
    assert "AAPL" in tickers
    assert "MSFT" in tickers


def test_fallback_sp500_contains_no_duplicates():
    tickers = _fallback_sp500()
    assert len(tickers) == len(set(tickers))


# ── get_sp500_tickers uses cache ──────────────────────────────────────────────

def test_sp500_tickers_uses_cache_when_available():
    cached = ["AAPL", "GOOGL"]
    with patch("src.data.universe.cache.get", return_value=cached):
        from src.data.universe import get_sp500_tickers
        result = get_sp500_tickers()
    assert result == cached


def test_sp500_tickers_falls_back_on_fetch_failure():
    with patch("src.data.universe.cache.get", return_value=None):
        with patch("src.data.universe.pd.read_html", side_effect=Exception("network")):
            from src.data.universe import get_sp500_tickers
            result = get_sp500_tickers()
    assert isinstance(result, list)
    assert len(result) > 0  # fallback list


# ── filter_universe ───────────────────────────────────────────────────────────

def test_filter_universe_price_filter():
    def mock_info(ticker):
        return {"regularMarketPrice": 5.0, "averageVolume": 500_000, "marketCap": 1e9}

    with patch("src.data.universe.fetcher.fetch_ticker_info", side_effect=mock_info):
        result = filter_universe(["AAPL", "MSFT"], min_price=10.0, min_avg_volume=100_000)

    assert result == []  # price 5.0 < min_price 10.0


def test_filter_universe_volume_filter():
    def mock_info(ticker):
        return {"regularMarketPrice": 100.0, "averageVolume": 50_000, "marketCap": 1e9}

    with patch("src.data.universe.fetcher.fetch_ticker_info", side_effect=mock_info):
        result = filter_universe(["AAPL"], min_price=1.0, min_avg_volume=200_000)

    assert result == []  # volume 50k < 200k


def test_filter_universe_passes_qualifying_tickers():
    def mock_info(ticker):
        return {"regularMarketPrice": 200.0, "averageVolume": 1_000_000, "marketCap": 5e9}

    with patch("src.data.universe.fetcher.fetch_ticker_info", side_effect=mock_info):
        with patch("src.data.universe.time.sleep"):
            result = filter_universe(["AAPL", "MSFT"], min_price=1.0, min_avg_volume=100_000)

    assert "AAPL" in result
    assert "MSFT" in result


def test_filter_universe_skips_on_fetch_error():
    with patch("src.data.universe.fetcher.fetch_ticker_info", side_effect=Exception("err")):
        result = filter_universe(["AAPL"], min_price=1.0, min_avg_volume=100_000)

    assert result == []  # errors silently skipped
