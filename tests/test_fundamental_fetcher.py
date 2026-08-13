"""Tests for fundamental fetcher key normalization."""

from src.fundamental.fetcher import fetch_fundamentals


def test_fetch_fundamentals_accepts_fast_info_style_keys():
    info = {
        "last_price": 25.0,
        "market_cap": 2_500_000_000,
        "shares": 100_000_000,
        "bookValue": 12.0,
    }

    fundamentals = fetch_fundamentals("AAP", info=info)

    assert fundamentals["ticker"] == "AAP"
    assert fundamentals["price"] == 25.0
    assert fundamentals["market_cap"] == 2_500_000_000
    assert fundamentals["shares_outstanding"] == 100_000_000
    assert fundamentals["total_equity"] == 1_200_000_000


# ── FMP fundamentals fallback ─────────────────────────────────────────────────

from unittest.mock import MagicMock, patch


def _fake_fmp_session(row):
    """A session whose .get(...).json() returns FMP's [{...}] quote shape."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = [row] if row is not None else []
    session = MagicMock()
    session.get.return_value = resp
    return session


_FMP_ROW = {"symbol": "AAPL", "price": 302.2, "marketCap": 4.6e12,
            "sharesOutstanding": 1.5e10, "eps": 6.5, "pe": 46.5}


def test_fmp_fills_market_cap_when_yfinance_returns_nothing():
    with patch("src.fundamental.fetcher.config.FMP_KEY", "test-key"), \
         patch("src.fundamental.fetcher.fetch_ticker_info", return_value={}), \
         patch("src.fundamental.fetcher.get_retry_session",
               return_value=_fake_fmp_session(_FMP_ROW)):
        f = fetch_fundamentals("AAPL")
    assert f["ticker"] == "AAPL"
    assert f["market_cap"] == 4.6e12
    assert f["price"] == 302.2
    assert f["pe_ratio"] == 46.5


def test_fmp_gapfills_when_info_present_but_market_cap_missing():
    info = {"currentPrice": 302.2}  # no marketCap
    with patch("src.fundamental.fetcher.config.FMP_KEY", "test-key"), \
         patch("src.fundamental.fetcher.get_retry_session",
               return_value=_fake_fmp_session(_FMP_ROW)):
        f = fetch_fundamentals("AAPL", info=info)
    assert f["market_cap"] == 4.6e12
    # yfinance value stays authoritative where present.
    assert f["price"] == 302.2


def test_no_fmp_call_when_yfinance_has_market_cap():
    info = {"market_cap": 2_500_000_000, "currentPrice": 25.0}
    session = _fake_fmp_session(_FMP_ROW)
    with patch("src.fundamental.fetcher.config.FMP_KEY", "test-key"), \
         patch("src.fundamental.fetcher.get_retry_session", return_value=session):
        f = fetch_fundamentals("AAP", info=info)
    assert f["market_cap"] == 2_500_000_000
    session.get.assert_not_called()   # no fallback needed → no FMP request


def test_no_fmp_call_without_key():
    session = _fake_fmp_session(_FMP_ROW)
    with patch("src.fundamental.fetcher.config.FMP_KEY", ""), \
         patch("src.fundamental.fetcher.fetch_ticker_info", return_value={}), \
         patch("src.fundamental.fetcher.get_retry_session", return_value=session):
        f = fetch_fundamentals("AAPL")
    assert f["market_cap"] is None
    session.get.assert_not_called()


def test_fmp_graceful_on_request_error():
    session = MagicMock()
    session.get.side_effect = RuntimeError("network down")
    with patch("src.fundamental.fetcher.config.FMP_KEY", "test-key"), \
         patch("src.fundamental.fetcher.fetch_ticker_info", return_value={"currentPrice": 10.0}), \
         patch("src.fundamental.fetcher.get_retry_session", return_value=session):
        f = fetch_fundamentals("AAPL")   # must not raise
    assert f["market_cap"] is None       # unfilled, but no crash
