"""OHLCV data fetcher — yfinance primary, with caching, retry, and parallel batch fetch."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from src.data import cache
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_RETRIES  = 3
_BACKOFF      = 2   # seconds
_BATCH_WORKERS = 8  # parallel threads for batch fetches


def fetch_ohlcv(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch OHLCV data for a ticker.

    Returns a DataFrame with columns: Open, High, Low, Close, Volume.
    Returns empty DataFrame on failure (allows callers to skip gracefully).
    """
    cache_key = f"ohlcv:{ticker}:{period}:{interval}"

    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    for attempt in range(_MAX_RETRIES):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                log.warning(f"{ticker}: empty OHLCV response")
                return pd.DataFrame()

            # Flatten MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

            if use_cache:
                cache.set(cache_key, df)

            return df

        except Exception as e:
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF ** attempt
                log.debug(f"{ticker}: fetch error ({e}), retrying in {wait}s")
                time.sleep(wait)
            else:
                log.error(f"{ticker}: failed to fetch OHLCV after {_MAX_RETRIES} attempts: {e}")
                return pd.DataFrame()

    return pd.DataFrame()


def fetch_ohlcv_batch(
    tickers: list,
    period: str = "3mo",
    interval: str = "1d",
    use_cache: bool = True,
    max_workers: int = _BATCH_WORKERS,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV data for multiple tickers in parallel.

    Returns a dict mapping ticker → DataFrame (empty DF on failure).
    """
    results: Dict[str, pd.DataFrame] = {}

    # Separate cache hits from tickers that need fetching
    need_fetch = []
    for ticker in tickers:
        cache_key = f"ohlcv:{ticker}:{period}:{interval}"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                results[ticker] = cached
                continue
        need_fetch.append(ticker)

    if not need_fetch:
        return results

    log.debug(f"Batch fetching {len(need_fetch)} tickers ({len(tickers) - len(need_fetch)} from cache) ...")

    def _fetch(ticker: str) -> tuple:
        return ticker, fetch_ohlcv(ticker, period=period, interval=interval, use_cache=use_cache)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch, t): t for t in need_fetch}
        for future in as_completed(futures):
            try:
                ticker, df = future.result()
                results[ticker] = df
            except Exception as e:
                ticker = futures[future]
                log.error(f"{ticker}: batch fetch error: {e}")
                results[ticker] = pd.DataFrame()

    return results


def fetch_ticker_info(ticker: str) -> dict:
    """Fetch yfinance .info dict for fundamental data. Returns {} on failure."""
    cache_key = f"info:{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        info = yf.Ticker(ticker).info
        cache.set(cache_key, info, ttl=3_600 * 6)  # 6-hour TTL for info
        return info
    except Exception as e:
        log.error(f"{ticker}: failed to fetch info: {e}")
        return {}


def get_current_price(ticker: str) -> Optional[float]:
    """Get latest closing price."""
    df = fetch_ohlcv(ticker, period="5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])
