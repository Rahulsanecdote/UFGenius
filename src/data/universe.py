"""Universe definition — S&P 500, Russell 1000, and filtering."""

import time
from typing import List, Optional

import pandas as pd
import requests

from src.data import cache, fetcher
from src.utils.logger import get_logger

log = get_logger(__name__)


def get_sp500_tickers() -> List[str]:
    """Fetch S&P 500 tickers from Wikipedia. Cached for 24h."""
    cached = cache.get("universe:sp500")
    if cached is not None:
        return cached

    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url)
        tickers = tables[0]["Symbol"].tolist()
        # Clean up tickers (replace . with - for yfinance)
        tickers = [t.replace(".", "-") for t in tickers]
        cache.set("universe:sp500", tickers)
        log.info(f"Loaded {len(tickers)} S&P 500 tickers")
        return tickers
    except Exception as e:
        log.error(f"Failed to fetch S&P 500 list: {e}")
        return _fallback_sp500()


def get_russell1000_tickers() -> List[str]:
    """Fetch Russell 1000 tickers from iShares IWB ETF holdings. Cached for 24h."""
    cached = cache.get("universe:russell1000")
    if cached is not None:
        return cached

    try:
        url = (
            "https://www.ishares.com/us/products/239707/ISHARES-RUSSELL-1000-ETF/"
            "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
        )
        df = pd.read_csv(url, skiprows=9)
        tickers = df["Ticker"].dropna().tolist()
        tickers = [str(t).strip() for t in tickers if str(t).strip() and str(t) != "nan"]
        cache.set("universe:russell1000", tickers)
        log.info(f"Loaded {len(tickers)} Russell 1000 tickers")
        return tickers
    except Exception as e:
        log.warning(f"Failed to fetch Russell 1000, falling back to S&P 500: {e}")
        return get_sp500_tickers()


def filter_universe(
    tickers: List[str],
    min_price: float = 1.0,
    min_avg_volume: int = 200_000,
    min_market_cap: Optional[int] = None,
) -> List[str]:
    """
    Apply basic filters to a ticker list using yfinance info.
    This is a slower, accurate filter — use for overnight scans.
    """
    passed = []
    for ticker in tickers:
        try:
            info = fetcher.fetch_ticker_info(ticker)
            price = info.get("regularMarketPrice") or info.get("currentPrice") or 0
            avg_vol = info.get("averageVolume", 0) or 0
            mkt_cap = info.get("marketCap", 0) or 0

            if price < min_price:
                continue
            if avg_vol < min_avg_volume:
                continue
            if min_market_cap and mkt_cap < min_market_cap:
                continue

            passed.append(ticker)
            time.sleep(0.05)  # Gentle rate limiting
        except Exception:
            continue

    log.info(f"Universe filter: {len(tickers)} → {len(passed)} tickers")
    return passed


def get_universe(universe: str = "SP500") -> List[str]:
    """Return ticker universe by name."""
    if universe == "SP500":
        return get_sp500_tickers()
    elif universe == "RUSSELL1000":
        return get_russell1000_tickers()
    else:
        log.warning(f"Unknown universe '{universe}', using SP500")
        return get_sp500_tickers()


def _fallback_sp500() -> List[str]:
    """Hardcoded top-50 S&P 500 tickers as emergency fallback."""
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK-B", "LLY", "AVGO",
        "JPM", "TSLA", "UNH", "V", "XOM", "MA", "JNJ", "COST", "HD", "PG",
        "ORCL", "ABBV", "BAC", "KO", "WMT", "NFLX", "CRM", "MRK", "CVX", "AMD",
        "PEP", "TMO", "ADBE", "ACN", "LIN", "TXN", "MCD", "CSCO", "ABT", "NEE",
        "PFE", "DHR", "UPS", "PM", "RTX", "SPGI", "AMGN", "GS", "INTC", "CAT",
    ]
