"""
OHLCV data fetcher — Alpaca primary with yfinance fallback,
plus caching, retry, timeout, and parallel batch fetch.

Compatible with yfinance 0.2.x AND 1.x.
"""

from __future__ import annotations

import re
import time as _time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from src.data import cache
from src.utils import config
from src.utils.http import get_retry_session, retry_call
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_RETRIES = max(0, config.REQUEST_MAX_RETRIES)
_BACKOFF = max(0.0, config.REQUEST_BACKOFF_SEC)
_YF_TIMEOUT = max(1.0, config.YFINANCE_TIMEOUT_SEC)
_BATCH_WORKERS = 8  # parallel threads for batch fetches
_STALE_CACHE_THRESHOLD_SEC = 3_600

_ALPACA_DATA_BASE_URL = config.env("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
_ALPACA_BASE_URL = config.env("ALPACA_BASE_URL", "").strip().rstrip("/")
_ALPACA_DATA_FEED = config.env("ALPACA_DATA_FEED", "iex").strip().lower() or "iex"
_ALPACA_TIMEFRAME_MAP = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "1d": "1Day",
}
_PERIOD_TOKEN_RE = re.compile(r"^(?P<value>\d+)(?P<unit>d|w|wk|mo|y)$", re.IGNORECASE)

_CRITICAL_CACHE_KEYS = {
    "AAPL": "info:AAPL",
    "SPY": "ohlcv:SPY:1y:1d",
    "^VIX": "ohlcv:^VIX:3mo:1d",
}

_REGIME_CACHE_KEYS = {
    "SPY": "ohlcv:SPY:1y:1d",
    "^VIX": "ohlcv:^VIX:3mo:1d",
    "TLT": "ohlcv:TLT:3mo:1d",
    "GLD": "ohlcv:GLD:3mo:1d",
}

# Detect yfinance major version once at import time
_YF_VERSION = getattr(yf, "__version__", "0.0.0")
_YF_MAJOR = int(_YF_VERSION.split(".")[0]) if _YF_VERSION else 0
log.debug(f"yfinance version: {_YF_VERSION} (major={_YF_MAJOR})")
_FAST_INFO_FAILURE_WARNED_SYMBOLS: set[str] = set()
_FAST_INFO_FAILURE_WARNED_LOCK = threading.Lock()


def _log_fast_info_failure(symbol: str, exc: Exception) -> None:
    """Warn once per symbol for fast_info access failures; debug thereafter."""
    first_occurrence = False
    with _FAST_INFO_FAILURE_WARNED_LOCK:
        if symbol not in _FAST_INFO_FAILURE_WARNED_SYMBOLS:
            _FAST_INFO_FAILURE_WARNED_SYMBOLS.add(symbol)
            first_occurrence = True
    if first_occurrence:
        log.warning(f"{symbol}: fast_info unavailable ({exc})")
        return
    log.debug(f"{symbol}: fast_info unavailable ({exc})")


def _alpaca_credentials_configured() -> bool:
    return bool(config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)


def _alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }


def _alpaca_trading_base_url() -> str:
    if _ALPACA_BASE_URL:
        return _ALPACA_BASE_URL
    return "https://paper-api.alpaca.markets" if config.ALPACA_PAPER else "https://api.alpaca.markets"


def _can_use_alpaca_symbol(symbol: str) -> bool:
    return bool(symbol) and not str(symbol).startswith("^")


def _period_to_timedelta(period: str) -> timedelta | None:
    period_value = str(period or "").strip().lower()
    if not period_value or period_value == "max":
        return None
    match = _PERIOD_TOKEN_RE.fullmatch(period_value)
    if not match:
        return None

    value = int(match.group("value"))
    unit = match.group("unit").lower()
    if unit == "d":
        return timedelta(days=value)
    if unit in {"w", "wk"}:
        return timedelta(weeks=value)
    if unit == "mo":
        return timedelta(days=value * 30)
    if unit == "y":
        return timedelta(days=value * 365)
    return None


def _iso_z(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_nonempty_dict(payload) -> bool:
    return isinstance(payload, dict) and bool(payload)


def _classify_provider_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return "RATE_LIMITED"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "AUTH_ERROR"
    if "not configured" in text:
        return "NOT_CONFIGURED"
    return "PROVIDER_ERROR"


def _provider_failure(provider: str, reason: str, detail: str | None = None) -> dict:
    item = {"provider": provider, "reason": reason}
    if detail:
        item["detail"] = detail[:240]
    return item


def _validate_ohlcv_frame(df: pd.DataFrame | None) -> tuple[pd.DataFrame, str | None]:
    """Normalize and validate OHLCV payload shape."""
    if df is None or df.empty:
        return pd.DataFrame(), "EMPTY_PAYLOAD"

    normalized = _normalise_columns(df.copy())
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing_cols = [c for c in required_cols if c not in normalized.columns]
    if missing_cols:
        return pd.DataFrame(), f"MISSING_COLUMNS:{','.join(missing_cols)}"

    cleaned = normalized[required_cols].dropna()
    if cleaned.empty:
        return pd.DataFrame(), "EMPTY_AFTER_NA_DROP"
    return cleaned, None


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


def _download_ohlcv_via_alpaca(
    symbol: str,
    *,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """
    Primary OHLCV source: Alpaca market data bars endpoint.

    Falls back to yfinance upstream when unsupported/unavailable.
    """
    if not _alpaca_credentials_configured():
        raise RuntimeError("Alpaca credentials are not configured")
    if not _can_use_alpaca_symbol(symbol):
        raise ValueError("Alpaca does not support this symbol format")

    timeframe = _ALPACA_TIMEFRAME_MAP.get(str(interval).lower())
    if timeframe is None:
        raise ValueError(f"Unsupported Alpaca interval: {interval}")

    delta = _period_to_timedelta(period)
    if delta is None:
        raise ValueError(f"Unsupported Alpaca period: {period}")

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - delta
    url = f"{_ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "start": _iso_z(start_ts),
        "end": _iso_z(end_ts),
        "adjustment": "all",
        "limit": 10_000,
        "sort": "asc",
        "feed": _ALPACA_DATA_FEED,
    }

    response = get_retry_session().get(
        url,
        headers=_alpaca_headers(),
        params=params,
        timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
    )
    response.raise_for_status()
    payload = response.json()
    bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(bars, list) or not bars:
        return pd.DataFrame()

    raw_df = pd.DataFrame.from_records(bars)
    if raw_df.empty:
        return pd.DataFrame()

    required_alpaca_cols = {"t", "o", "h", "l", "c", "v"}
    if not required_alpaca_cols.issubset(set(raw_df.columns)):
        return pd.DataFrame()

    df = raw_df.rename(
        columns={
            "o": "Open",
            "h": "High",
            "l": "Low",
            "c": "Close",
            "v": "Volume",
        }
    )
    timestamps = pd.to_datetime(df["t"], utc=True, errors="coerce")
    df = df.drop(columns=["t"])
    df.index = timestamps
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert(None)
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
    Try Alpaca first (if configured/supported), then yfinance:
    Ticker.history() first, yf.download() fallback.
    """
    can_try_alpaca = (
        _alpaca_credentials_configured()
        and _can_use_alpaca_symbol(symbol)
        and str(interval).lower() in _ALPACA_TIMEFRAME_MAP
        and _period_to_timedelta(period) is not None
    )
    if can_try_alpaca:
        try:
            df = _download_ohlcv_via_alpaca(symbol, period=period, interval=interval)
            if df is not None and not df.empty:
                return df
            log.warning(f"{symbol}: Alpaca returned empty OHLCV, falling back to yfinance")
        except Exception as exc:
            log.warning(f"{symbol}: Alpaca OHLCV failed ({exc}), falling back to yfinance")

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
        _log_fast_info_failure(symbol, exc)
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


def _fetch_ticker_info_via_alpaca_once(ticker: str) -> dict:
    symbol = ticker.upper()
    if not _alpaca_credentials_configured():
        raise RuntimeError("Alpaca credentials are not configured")
    if not _can_use_alpaca_symbol(symbol):
        raise ValueError("Alpaca does not support this symbol format")

    session = get_retry_session()
    asset_resp = session.get(
        f"{_alpaca_trading_base_url()}/v2/assets/{symbol}",
        headers=_alpaca_headers(),
        timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
    )
    if asset_resp.status_code == 404:
        return {}
    asset_resp.raise_for_status()
    asset = asset_resp.json()
    if not isinstance(asset, dict):
        return {}

    info: dict = {
        "symbol": symbol,
        "longName": asset.get("name"),
        "exchange": asset.get("exchange"),
        "quoteType": "EQUITY",
        "currency": "USD",
        "tradable": asset.get("tradable"),
        "marginable": asset.get("marginable"),
        "shortable": asset.get("shortable"),
        "fractionable": asset.get("fractionable"),
        "status": asset.get("status"),
    }

    try:
        snapshot_resp = session.get(
            f"{_ALPACA_DATA_BASE_URL}/v2/stocks/snapshots",
            headers=_alpaca_headers(),
            params={"symbols": symbol, "feed": _ALPACA_DATA_FEED},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        if snapshot_resp.ok:
            payload = snapshot_resp.json()
            snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
            snapshot = snapshots.get(symbol) if isinstance(snapshots, dict) else None
            if isinstance(snapshot, dict):
                latest_trade = snapshot.get("latestTrade") if isinstance(snapshot.get("latestTrade"), dict) else {}
                daily_bar = snapshot.get("dailyBar") if isinstance(snapshot.get("dailyBar"), dict) else {}
                prev_daily = snapshot.get("prevDailyBar") if isinstance(snapshot.get("prevDailyBar"), dict) else {}

                current_price = _as_float(latest_trade.get("p")) or _as_float(daily_bar.get("c"))
                previous_close = _as_float(prev_daily.get("c"))
                daily_volume = _as_float(daily_bar.get("v"))

                if current_price is not None:
                    info["currentPrice"] = current_price
                    info["regularMarketPrice"] = current_price
                if previous_close is not None:
                    info["previousClose"] = previous_close
                if daily_volume is not None:
                    info["averageVolume"] = daily_volume
    except Exception as exc:
        log.debug(f"{symbol}: Alpaca snapshot enrichment failed ({exc})")

    return {k: v for k, v in info.items() if v is not None}


# ── Public API ───────────────────────────────────────────────────────────────


def _fetch_ticker_info_with_diagnostics(
    ticker: str,
    *,
    use_cache: bool = True,
    allow_stale: bool = True,
) -> tuple[dict, dict]:
    """
    Fetch ticker info with provider trace metadata for diagnostics.

    Returns (info_dict, diagnostics_dict). info_dict is always a dict.
    """
    symbol = ticker.upper()
    cache_key = f"info:{symbol}"
    failures: list[dict] = []

    if use_cache:
        cached = cache.get(cache_key)
        if _is_nonempty_dict(cached):
            return cached, {
                "status": "OK",
                "source": "cache",
                "reason": None,
                "provider_failures": failures,
            }
        if isinstance(cached, dict) and not cached:
            failures.append(_provider_failure("cache", "EMPTY_CACHED_PAYLOAD_IGNORED"))
        elif cached is not None:
            failures.append(
                _provider_failure(
                    "cache",
                    "INVALID_CACHED_PAYLOAD_IGNORED",
                    detail=f"type={type(cached).__name__}",
                )
            )

    can_try_alpaca = _alpaca_credentials_configured() and _can_use_alpaca_symbol(symbol)
    if can_try_alpaca:
        try:
            info = retry_call(
                _fetch_ticker_info_via_alpaca_once,
                symbol,
                retries=_MAX_RETRIES,
                backoff=_BACKOFF,
            )
            if _is_nonempty_dict(info):
                cache.set(cache_key, info, ttl=3_600 * 6)
                return info, {
                    "status": "OK",
                    "source": "alpaca",
                    "reason": None,
                    "provider_failures": failures,
                }
            if info is None:
                failures.append(_provider_failure("alpaca", "NONE_PAYLOAD"))
            elif not isinstance(info, dict):
                failures.append(
                    _provider_failure(
                        "alpaca",
                        "NON_DICT_PAYLOAD",
                        detail=f"type={type(info).__name__}",
                    )
                )
            else:
                failures.append(_provider_failure("alpaca", "EMPTY_PAYLOAD"))
        except Exception as exc:
            failures.append(
                _provider_failure("alpaca", _classify_provider_exception(exc), detail=str(exc))
            )
    else:
        if not _alpaca_credentials_configured():
            failures.append(_provider_failure("alpaca", "NOT_CONFIGURED"))
        elif not _can_use_alpaca_symbol(symbol):
            failures.append(_provider_failure("alpaca", "UNSUPPORTED_SYMBOL"))

    try:
        info = retry_call(
            _fetch_ticker_info_once,
            symbol,
            retries=_MAX_RETRIES,
            backoff=_BACKOFF,
        )
        if _is_nonempty_dict(info):
            cache.set(cache_key, info, ttl=3_600 * 6)
            return info, {
                "status": "OK",
                "source": "yfinance",
                "reason": None,
                "provider_failures": failures,
            }
        if info is None:
            failures.append(_provider_failure("yfinance", "NONE_PAYLOAD"))
        elif not isinstance(info, dict):
            failures.append(
                _provider_failure(
                    "yfinance",
                    "NON_DICT_PAYLOAD",
                    detail=f"type={type(info).__name__}",
                )
            )
        else:
            failures.append(_provider_failure("yfinance", "EMPTY_PAYLOAD"))
    except Exception as exc:
        failures.append(
            _provider_failure("yfinance", _classify_provider_exception(exc), detail=str(exc))
        )

    if allow_stale:
        stale = _fallback_to_stale_cache(cache_key, symbol=symbol, label="ticker info")
        if _is_nonempty_dict(stale):
            return stale, {
                "status": "OK",
                "source": "stale_cache",
                "reason": "STALE_CACHE_FALLBACK",
                "provider_failures": failures,
            }

    return {}, {
        "status": "EMPTY",
        "source": None,
        "reason": "ALL_PROVIDERS_FAILED_OR_EMPTY",
        "provider_failures": failures,
    }


def _probe_ohlcv_live(
    symbol: str,
    *,
    period: str = "5d",
    interval: str = "1d",
) -> tuple[pd.DataFrame, dict]:
    """
    Probe live OHLCV providers without cache/stale fallbacks.

    Returns (cleaned_df, diagnostics_dict).
    """
    ticker = symbol.upper()
    failures: list[dict] = []

    can_try_alpaca = (
        _alpaca_credentials_configured()
        and _can_use_alpaca_symbol(ticker)
        and str(interval).lower() in _ALPACA_TIMEFRAME_MAP
        and _period_to_timedelta(period) is not None
    )
    if can_try_alpaca:
        try:
            raw = _download_ohlcv_via_alpaca(ticker, period=period, interval=interval)
            cleaned, reason = _validate_ohlcv_frame(raw)
            if not cleaned.empty:
                return cleaned, {
                    "status": "OK",
                    "source": "alpaca",
                    "reason": None,
                    "provider_failures": failures,
                }
            failures.append(_provider_failure("alpaca", reason or "EMPTY_PAYLOAD"))
        except Exception as exc:
            failures.append(
                _provider_failure("alpaca", _classify_provider_exception(exc), detail=str(exc))
            )
    else:
        if not _alpaca_credentials_configured():
            failures.append(_provider_failure("alpaca", "NOT_CONFIGURED"))
        elif not _can_use_alpaca_symbol(ticker):
            failures.append(_provider_failure("alpaca", "UNSUPPORTED_SYMBOL"))
        else:
            failures.append(_provider_failure("alpaca", "UNSUPPORTED_INTERVAL_OR_PERIOD"))

    try:
        raw = _download_ohlcv_via_ticker(ticker, period=period, interval=interval)
        cleaned, reason = _validate_ohlcv_frame(raw)
        if not cleaned.empty:
            return cleaned, {
                "status": "OK",
                "source": "yfinance_ticker",
                "reason": None,
                "provider_failures": failures,
            }
        failures.append(_provider_failure("yfinance_ticker", reason or "EMPTY_PAYLOAD"))
    except Exception as exc:
        failures.append(
            _provider_failure("yfinance_ticker", _classify_provider_exception(exc), detail=str(exc))
        )

    try:
        raw = _download_ohlcv_via_download(ticker, period=period, interval=interval)
        cleaned, reason = _validate_ohlcv_frame(raw)
        if not cleaned.empty:
            return cleaned, {
                "status": "OK",
                "source": "yfinance_download",
                "reason": None,
                "provider_failures": failures,
            }
        failures.append(_provider_failure("yfinance_download", reason or "EMPTY_PAYLOAD"))
    except Exception as exc:
        failures.append(
            _provider_failure("yfinance_download", _classify_provider_exception(exc), detail=str(exc))
        )

    return pd.DataFrame(), {
        "status": "EMPTY",
        "source": None,
        "reason": "ALL_PROVIDERS_FAILED_OR_EMPTY",
        "provider_failures": failures,
    }


def clear_data_caches() -> None:
    """Clear all on-disk cache entries used by data/fundamental fetch paths."""
    cache.clear_all()


def _fallback_to_stale_cache(cache_key: str, *, symbol: str, label: str):
    stale = cache.get_stale(cache_key)
    if stale is not None:
        log.warning(f"{symbol}: using stale cached {label} due to upstream fetch failure")
        return stale
    return None


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
        stale = _fallback_to_stale_cache(cache_key, symbol=symbol, label="OHLCV")
        return stale if isinstance(stale, pd.DataFrame) else pd.DataFrame()
    except Exception as exc:
        log.error(f"{symbol}: OHLCV fetch failed after {_MAX_RETRIES + 1} attempts: {exc}")
        stale = _fallback_to_stale_cache(cache_key, symbol=symbol, label="OHLCV")
        return stale if isinstance(stale, pd.DataFrame) else pd.DataFrame()

    cleaned, invalid_reason = _validate_ohlcv_frame(df)
    if cleaned.empty:
        if invalid_reason == "EMPTY_PAYLOAD":
            log.warning(f"{symbol}: empty OHLCV response from providers")
        elif invalid_reason and invalid_reason.startswith("MISSING_COLUMNS:"):
            missing_cols = invalid_reason.split(":", 1)[1].split(",")
            log.warning(f"{symbol}: OHLCV missing columns {missing_cols} (provider payload invalid)")
        elif invalid_reason == "EMPTY_AFTER_NA_DROP":
            log.warning(f"{symbol}: all OHLCV rows dropped after NaN removal")
        else:
            log.warning(f"{symbol}: invalid OHLCV payload ({invalid_reason})")
        stale = _fallback_to_stale_cache(cache_key, symbol=symbol, label="OHLCV")
        return stale if isinstance(stale, pd.DataFrame) else pd.DataFrame()

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
    """
    Fetch ticker metadata/fundamentals with Alpaca primary and yfinance fallback.

    Empty payloads are treated as failures and are never written to cache.
    """
    symbol = ticker.upper()
    info, diag = _fetch_ticker_info_with_diagnostics(symbol, use_cache=True, allow_stale=True)

    if _is_nonempty_dict(info):
        return info

    reason = diag.get("reason") or "UNKNOWN"
    failures = diag.get("provider_failures") or []
    if failures:
        log.warning(f"{symbol}: ticker info unavailable ({reason}) after {len(failures)} provider failures")
    else:
        log.warning(f"{symbol}: ticker info unavailable ({reason})")
    return {}


def get_fundamentals(ticker: str) -> dict:
    """
    Fetch fundamentals payload from the provider in a defensive way.

    Always returns a dictionary and never raises on provider payload shape issues.
    """
    symbol = ticker.upper()
    fundamentals: dict = {}

    try:
        provider_data = fetch_ticker_info(symbol)
    except Exception as exc:
        log.warning(f"{symbol}: fundamentals fetch failed ({exc})")
        return {}

    fundamentals = fundamentals or {}
    if provider_data is None:
        log.warning(f"{symbol}: fundamentals provider returned None")
    elif not isinstance(provider_data, dict):
        log.warning(f"{symbol}: fundamentals provider returned non-dict payload")
    elif provider_data:
        fundamentals.update(provider_data)
    else:
        log.warning(f"{symbol}: fundamentals provider returned empty payload")

    return fundamentals if fundamentals else {}


def get_current_price(ticker: str) -> Optional[float]:
    """Get latest closing price."""
    df = fetch_ohlcv(ticker, period="5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _format_cache_age(age_sec: float | None) -> str:
    if age_sec is None:
        return "Unknown"
    total = int(max(0, age_sec))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _cache_freshness_for_keys(
    symbol_to_key: dict[str, str],
    *,
    stale_threshold_sec: int = _STALE_CACHE_THRESHOLD_SEC,
) -> dict:
    symbols: dict[str, dict] = {}
    max_age_sec: float | None = None
    any_stale = False

    for symbol, cache_key in symbol_to_key.items():
        meta = cache.get_metadata(cache_key, allow_expired=True)
        age_sec: float | None = None
        is_expired = False
        has_cache = meta is not None

        if isinstance(meta, dict):
            raw_age = meta.get("age_sec")
            if isinstance(raw_age, (int, float)):
                age_sec = float(raw_age)
            is_expired = bool(meta.get("is_expired"))

        is_stale = age_sec is not None and age_sec > stale_threshold_sec
        any_stale = any_stale or is_stale
        if age_sec is not None and (max_age_sec is None or age_sec > max_age_sec):
            max_age_sec = age_sec

        symbols[symbol] = {
            "cache_key": cache_key,
            "has_cache": has_cache,
            "is_expired": is_expired,
            "is_stale": is_stale,
            "age_sec": round(age_sec, 2) if age_sec is not None else None,
            "age_human": _format_cache_age(age_sec),
        }

    return {
        "symbols": symbols,
        "stale_threshold_sec": stale_threshold_sec,
        "any_stale": any_stale,
        "max_age_sec": round(max_age_sec, 2) if max_age_sec is not None else None,
        "max_age_human": _format_cache_age(max_age_sec),
    }


def get_critical_cache_freshness(max_age_sec: int = _STALE_CACHE_THRESHOLD_SEC) -> dict:
    snapshot = _cache_freshness_for_keys(
        _CRITICAL_CACHE_KEYS,
        stale_threshold_sec=max_age_sec,
    )
    snapshot["any_critical_stale"] = snapshot["any_stale"]
    return snapshot


def get_regime_cache_freshness(max_age_sec: int = _STALE_CACHE_THRESHOLD_SEC) -> dict:
    snapshot = _cache_freshness_for_keys(
        _REGIME_CACHE_KEYS,
        stale_threshold_sec=max_age_sec,
    )
    snapshot["any_regime_stale"] = snapshot["any_stale"]
    return snapshot


# ── Diagnostics ──────────────────────────────────────────────────────────────

def diagnose() -> dict:
    """
    Run a quick health check on market data connectivity.
    Call this to debug data issues.
    """
    import sys

    results = {
        "python": sys.version,
        "yfinance_version": _YF_VERSION,
        "pandas_version": pd.__version__,
        "tests": {},
        "fundamentals": {},
    }

    for symbol in ["AAPL", "SPY", "^VIX"]:
        start = _time.time()
        try:
            df, probe = _probe_ohlcv_live(symbol, period="5d", interval="1d")
            elapsed = round(_time.time() - start, 2)
            if df is not None and not df.empty:
                results["tests"][symbol] = {
                    "status": "OK",
                    "rows": len(df),
                    "columns": list(df.columns),
                    "last_close": round(float(df["Close"].iloc[-1]), 2),
                    "provider": probe.get("source"),
                    "elapsed_sec": elapsed,
                }
            else:
                results["tests"][symbol] = {
                    "status": "EMPTY",
                    "reason": probe.get("reason") or "UNKNOWN",
                    "provider_failures": probe.get("provider_failures", []),
                    "elapsed_sec": elapsed,
                }
        except Exception as e:
            elapsed = round(_time.time() - start, 2)
            results["tests"][symbol] = {
                "status": "ERROR",
                "error": str(e),
                "elapsed_sec": elapsed,
            }

    start = _time.time()
    try:
        fund = get_fundamentals("AAPL")
        is_valid = isinstance(fund, dict)
        has_data = bool(fund) if is_valid else False
        elapsed = round(_time.time() - start, 2)
        if is_valid and has_data:
            market_cap = fund.get("marketCap")
            if market_cap is None:
                market_cap = fund.get("market_cap")
            results["fundamentals"] = {
                "status": "OK",
                "market_cap": market_cap,
                "keys": sorted(list(fund.keys()))[:12],
                "elapsed_sec": elapsed,
            }
        else:
            _, fund_diag = _fetch_ticker_info_with_diagnostics(
                "AAPL",
                use_cache=False,
                allow_stale=False,
            )
            results["fundamentals"] = {
                "status": "EMPTY",
                "reason": fund_diag.get("reason") or "UNKNOWN",
                "provider_failures": fund_diag.get("provider_failures", []),
                "elapsed_sec": elapsed,
            }
    except Exception as e:
        elapsed = round(_time.time() - start, 2)
        results["fundamentals"] = {
            "status": "ERROR",
            "error": str(e),
            "elapsed_sec": elapsed,
        }

    price_ok = all(t["status"] == "OK" for t in results["tests"].values())
    fundamentals_ok = results["fundamentals"].get("status") == "OK"
    all_ok = price_ok and fundamentals_ok
    results["overall"] = "HEALTHY" if all_ok else "DEGRADED"
    try:
        results["cache_freshness"] = get_critical_cache_freshness()
    except Exception as exc:
        log.debug(f"diagnose: cache freshness snapshot failed ({exc})")
        results["cache_freshness"] = {
            "symbols": {},
            "stale_threshold_sec": _STALE_CACHE_THRESHOLD_SEC,
            "any_stale": False,
            "any_critical_stale": False,
            "max_age_sec": None,
            "max_age_human": "Unknown",
        }
    return results
