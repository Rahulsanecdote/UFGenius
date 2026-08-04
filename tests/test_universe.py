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
        with patch("src.data.universe.http.get_text", side_effect=Exception("network")):
            from src.data.universe import get_sp500_tickers
            result = get_sp500_tickers()
    assert isinstance(result, list)
    assert len(result) > 0  # fallback list


# ── hardened constituent fetches (audit M10) ─────────────────────────────────

_SP500_HTML = """
<html><body>
<table>
  <tr><th>Rank</th><th>Notes</th></tr>
  <tr><td>1</td><td>not the constituents table</td></tr>
</table>
<table>
  <tr><th>Symbol</th><th>Security</th></tr>
  <tr><td>AAPL</td><td>Apple</td></tr>
  <tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr>
</table>
</body></html>
"""


def test_sp500_selects_table_by_symbol_header_not_position():
    from src.data.universe import get_sp500_tickers

    with patch("src.data.universe.cache.get", return_value=None), \
         patch("src.data.universe.cache.set"), \
         patch("src.data.universe.http.get_text", return_value=_SP500_HTML) as mock_get:
        result = get_sp500_tickers()

    mock_get.assert_called_once()  # fetch goes through utils/http (timeout+retry)
    assert result == ["AAPL", "BRK-B"]  # dot mapped to dash for yfinance


_IWB_CSV = "\n".join(
    [
        "iShares Russell 1000 ETF",
        "Fund Holdings as of,\"Aug 01, 2026\"",
        "Inception Date,\"May 15, 2000\"",
        "",  # preamble length varies — header is NOT at a fixed row
        "Ticker,Name,Sector,Asset Class",
        "AAPL,APPLE INC,Information Technology,Equity",
        "MSFT,MICROSOFT CORP,Information Technology,Equity",
        ",,,",
    ]
)


def test_russell1000_locates_header_row_by_content():
    from src.data.universe import get_russell1000_tickers

    with patch("src.data.universe.cache.get", return_value=None), \
         patch("src.data.universe.cache.set"), \
         patch("src.data.universe.http.get_text", return_value=_IWB_CSV):
        result = get_russell1000_tickers()

    assert result == ["AAPL", "MSFT"]


def test_russell1000_falls_back_to_sp500_when_header_missing():
    from src.data.universe import get_russell1000_tickers

    with patch("src.data.universe.cache.get", return_value=None), \
         patch("src.data.universe.http.get_text", return_value="no,header,here"), \
         patch("src.data.universe.get_sp500_tickers", return_value=["AAPL"]) as mock_sp:
        result = get_russell1000_tickers()

    mock_sp.assert_called_once()
    assert result == ["AAPL"]


def test_locate_csv_header_handles_quoted_fields():
    from src.data.universe import _locate_csv_header

    text = 'preamble\n"Ticker","Name"\nAAPL,Apple'
    assert _locate_csv_header(text, "Ticker") == 1


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


def test_fetch_user_agent_comes_from_config():
    import src.utils.config as cfg

    from src.data.universe import _UA_HEADERS

    assert _UA_HEADERS["User-Agent"] == cfg.CONSTITUENT_FETCH_USER_AGENT
    assert cfg.CONSTITUENT_FETCH_USER_AGENT  # non-empty default
