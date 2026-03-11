"""
OHLCV data fetcher — yfinance primary, with caching, retry, timeout,
and parallel batch fetch.

Compatible with yfinance 0.2.x AND 1.x.
"""

from __future__ import annotations

import time as _time
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

# Detect yfinance major version once at import time
_YF_VERSION = getattr(yf, "__version__", "0.0.0")
_YF_MAJOR = int(_YF_VERSION.split(".")[0]) if _YF_VERSION else 0
log.debug(f"yfinance version: {_YF_VERSION} (major={_YF_MAJOR})")


# ── Low-level fetch helpers ──────────────────────────────────────────────────

def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flatten MultiIndex columns and title-case names.

    yfinance ≥0.2.40 and all 1.x versions return MultiIndex columns
    from yf.download() — e.g. ('Close', 'AAPL').  Ticker.history()
    returns flat columns.  This handles both.
    """
    if isinstance(df.columns, pd.MultiIndex):
        # Try level 0 first (usually the field name)
        df.columns = df.columns.get_level_values(0)
    # Normalise to Title Case (Open, High, Low, Close, Volume)
    df.columns = [str(c).strip().title() for c in df.columns]
    # De-duplicate in case MultiIndex had repeated field names
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]
    return df


def _download_ohlcv_via_ticker(
    symbol: str,
    *,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """
    Use yf.Ticker().history() — more reliable across yfinance versions
    and avoids the MultiIndex quirks of yf.download().
    """
    t = yf.Ticker(symbol)
    df = t.history(period=period, interval=interval, auto_adjust=True)
    return df


def _download_ohlcv_via_download(
    symbol: str,
    *,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """Fallback: use yf.download() if Ticker.history() fails."""
    kwargs = dict(
        tickers=symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
    )
    # timeout kwarg only supported in some yfinance versions
    try:
        df = yf.download(**kwargs, timeout=_YF_TIMEOUT)
    except TypeError:
        df = yf.download(**kwargs)
    return df


def _download_ohlcv_once(
    symbol: str,
    *,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """
    Try Ticker.history() first (most reliable), fall back to yf.download().
    """
    try:
        df = _download_ohlcv_via_ticker(symbol, period=period, interval=interval)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        log.debug(f"{symbol}: Ticker.history() failed ({e}), trying yf.download()")

    return _download_ohlcv_via_download(symbol, period=period, interval=interval)


def _merge_fast_info_fields(info: dict, ticker_obj: yf.Ticker, symbol: str) -> dict:
    """Backfill brittle .info fields from yfinance fast_info when available."""
    try:
        fast_info = ticker_obj.fast_info
    except Exception as exc:
        log.debug(f"{symbol}: fast_info unavailable ({exc})")
        return info

    field_map = {
        "marketCap": ("marketCap", "market_cap"),
        "sharesOutstanding": ("sharesOutstanding", "shares"),
        "currentPrice": ("currentPrice", "lastPrice", "last_price"),
        "regularMarketPrice": ("regularMarketPrice", "lastPrice", "last_price"),
        "previousClose": ("previousClose", "previous_close"),
        "currency": ("currency",),
        "exchange": ("exchange",),
        "quoteType": ("quoteType", "quote_type"),
    }

    for target_key, fast_keys in field_map.items():
        if info.get(target_key) is not None:
            continue
        for fast_key in fast_keys:
            try:
                value = fast_info.get(fast_key)
            except Exception as exc:
                log.debug(f"{symbol}: fast_info[{fast_key}] failed ({exc})")
                value = None
            if value is not None:
                info[target_key] = value
                break
    return info


def _fetch_ticker_info_once(ticker: str) -> dict:
    symbol = ticker.upper()
    t = yf.Ticker(symbol)

    try:
        info = t.info
    except Exception as exc:
        log.debug(f"{symbol}: Ticker.info failed ({exc}), falling back to fast_info")
        info = {}

    if info is None:
        info = {}
    if not isinstance(info, dict):
        info = {}

    return _merge_fast_info_fields(dict(info), t, symbol)


# ── Public API ───────────────────────────────────────────────────────────────

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
        log.error(f"{symbol}: OHLCV fetch timed out after {_YF_TIMEOUT}s")
        return pd.DataFrame()
    except Exception as exc:
        log.error(f"{symbol}: OHLCV fetch failed after {_MAX_RETRIES + 1} attempts: {exc}")
        return pd.DataFrame()

    if df is None or df.empty:
        log.warning(f"{symbol}: empty OHLCV response from yfinance {_YF_VERSION}")
        return pd.DataFrame()

    # Normalise column names across yfinance versions
    df = _normalise_columns(df)

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        log.warning(f"{symbol}: OHLCV missing columns {missing_cols} (got: {list(df.columns)})")
        return pd.DataFrame()

    cleaned = df[required_cols].dropna()
    if cleaned.empty:
        log.warning(f"{symbol}: all rows dropped after NaN removal")
        return pd.DataFrame()

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


# ── Diagnostics ──────────────────────────────────────────────────────────────

def diagnose() -> dict:
    """
    Run a quick health check on yfinance connectivity.
    Call this to debug data issues.
    """
    import sys

    results = {
        "python": sys.version,
        "yfinance_version": _YF_VERSION,
        "pandas_version": pd.__version__,
        "tests": {},
    }

    for symbol in ["AAPL", "SPY", "^VIX"]:
        start = _time.time()
        try:
            t = yf.Ticker(symbol)
            df = t.history(period="5d", auto_adjust=True)
            elapsed = round(_time.time() - start, 2)
            if df is not None and not df.empty:
                results["tests"][symbol] = {
                    "status": "OK",
                    "rows": len(df),
                    "columns": list(df.columns),
                    "last_close": round(float(df["Close"].iloc[-1]), 2),
                    "elapsed_sec": elapsed,
                }
            else:
                results["tests"][symbol] = {
                    "status": "EMPTY",
                    "elapsed_sec": elapsed,
                }
        except Exception as e:
            elapsed = round(_time.time() - start, 2)
            results["tests"][symbol] = {
                "status": "ERROR",
                "error": str(e),
                "elapsed_sec": elapsed,
            }

    all_ok = all(t["status"] == "OK" for t in results["tests"].values())
    results["overall"] = "HEALTHY" if all_ok else "DEGRADED"
    return results
