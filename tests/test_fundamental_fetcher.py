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
