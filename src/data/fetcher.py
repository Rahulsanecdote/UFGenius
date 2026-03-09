"""OHLCV data fetcher — yfinance primary, with caching, retry, timeout, and parallel batch fetch."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from src.data import cache
from src.utils import config
from src.utils.http import retry_call
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_RETRIES = max(0, config.REQUEST_MAX_RETRIES)
_BACKOFF = max(0.0, config.REQUEST_BACKOFF_SEC)
_YF_TIMEOUT = max(1.0, config.YFINANCE_TIMEOUT_SEC)
_BATCH_WORKERS = 8  # parallel threads for batch fetches


def _call_with_timeout(fn, *, timeout_sec: float):
    with ThreadPoolExecutor(max_workers=1) as executor:
        fut = executor.submit(fn)
        return fut.result(timeout=timeout_sec)


def _download_ohlcv_once(
    ticker: str,
    *,
    period: str,
    interval: str,
) -> pd.DataFrame:
    # yfinance supports timeout in some paths; we enforce an upper bound regardless
    # via a guarded future to avoid hung calls.
    return _call_with_timeout(
        lambda: yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            timeout=_YF_TIMEOUT,
        ),
        timeout_sec=_YF_TIMEOUT,
    )


def _fetch_ticker_info_once(ticker: str) -> dict:
    return _call_with_timeout(
        lambda: yf.Ticker(ticker).info,
        timeout_sec=_YF_TIMEOUT,
    )


def clear_data_caches() -> None:
    """Clear all on-disk cache entries used by data/fundamental fetch paths."""
    cache.clear_all()


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
    symbol = ticker.upper()
    cache_key = f"ohlcv:{symbol}:{period}:{interval}"

    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        df = retry_call(
            _download_ohlcv_once,
            symbol,
            period=period,
            interval=interval,
            retries=_MAX_RETRIES,
            backoff=_BACKOFF,
        )
    except TimeoutError:
        log.error(f"{symbol}: OHLCV fetch timed out")
        return pd.DataFrame()
    except Exception as exc:
        log.error(f"{symbol}: failed to fetch OHLCV after {_MAX_RETRIES + 1} attempts: {exc}")
        return pd.DataFrame()

    if df.empty:
        log.warning(f"{symbol}: empty OHLCV response")
        return pd.DataFrame()

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        log.warning(f"{symbol}: OHLCV response missing columns: {missing_cols}")
        return pd.DataFrame()

    cleaned = df[required_cols].dropna()
    if use_cache:
        cache.set(cache_key, cleaned)
    return cleaned


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
    symbols = [str(t).upper() for t in tickers]

    # Separate cache hits from tickers that need fetching
    need_fetch = []
    for ticker in symbols:
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
    symbol = ticker.upper()
    cache_key = f"info:{symbol}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        info = retry_call(
            _fetch_ticker_info_once,
            symbol,
            retries=_MAX_RETRIES,
            backoff=_BACKOFF,
        )
        if not isinstance(info, dict):
            log.warning(f"{symbol}: ticker info payload was not a dict")
            return {}
        cache.set(cache_key, info, ttl=3_600 * 6)  # 6-hour TTL for info
        return info
    except TimeoutError:
        log.error(f"{symbol}: ticker info fetch timed out")
        return {}
    except Exception as e:
        log.error(f"{symbol}: failed to fetch info: {e}")
        return {}


def get_current_price(ticker: str) -> Optional[float]:
    """Get latest closing price."""
    df = fetch_ohlcv(ticker, period="5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])
