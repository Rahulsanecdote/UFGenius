"""Market cap the hard filters can actually get.

The ``UNKNOWN_MARKET_CAP`` disqualifier (``signals/filters.py``) fails closed,
and it should: a ticker whose size cannot be verified must not trade. But on the
Render paper trader's first scheduled scan (2026-10-01 21:00 ET) it rejected
**every** candidate, ABBV and AMAT included. Nothing could answer. With Alpaca
keys set, ``fetch_ticker_info`` returns Alpaca's asset record, which has no
market-cap field, so yfinance is never asked; FMP needs a key the worker did not
have; Finviz is off by default. A filter that rejects everything is not a safety
rail, it is an outage that looks like a quiet market.

Two sources sit behind the existing ones (``fundamental/fetcher.py`` decides the
order):

1. **The last known value, on disk** (``mcap:{TICKER}``, ``market_cap.
   cache_hours``, default 72). The filter checks a size *floor* ($100M); a value
   a few days old answers that as well as a fresh one, and on the worker
   ``data/`` is a persistent disk, so one good answer survives restarts and
   deploys.
2. **SEC EDGAR, keyless: shares outstanding × price.** First the cover-page
   ``dei:EntityCommonStockSharesOutstanding``; if that is absent, the
   ``us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`` total. The second
   exists because multi-class issuers report cover-page shares per class, as
   dimensional facts the companyconcept API omits — Alphabet returns 404 for the
   first and 12.31B diluted shares for the second. It is an approximation (a
   weighted average; one quarter stale at most), which is why the result carries
   ``market_cap_source`` and is only used where a floor is being checked.

SEC asks automated clients to send a User-Agent naming a contact
(``SEC_USER_AGENT``) and to stay under 10 requests a second; requests here are
serialised behind a minimum interval and the CIK map and share counts are cached
for days. Nothing in this module raises.
"""

from __future__ import annotations

import math
import threading
import time
from typing import NamedTuple, Optional

from src.data import cache
from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

__all__ = ["KnownCap", "cached", "remember", "sec_market_cap"]

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"
# Cover-page shares first (exact, current); the diluted weighted average is the
# one non-dimensional total multi-class issuers still report.
_SHARE_CONCEPTS = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
)

_CIK_MAP_KEY = "sec:cik_map"
_CIK_MAP_TTL = 7 * 24 * 3600
_SHARES_TTL = 7 * 24 * 3600          # filed quarterly; a week loses nothing
_NO_SHARES_TTL = 24 * 3600           # don't re-ask SEC hourly about a ticker it can't answer
_MIN_INTERVAL_SEC = 0.15             # well under SEC's 10 requests/second
_FAILURE_BACKOFF_SEC = 1800.0

_lock = threading.Lock()
_last_request = 0.0
_retry_after = 0.0


class KnownCap(NamedTuple):
    value: float
    source: str
    age_hours: float


def _valid(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _norm(ticker: str) -> str:
    """SEC writes class shares with a hyphen (BRK-B); Alpaca with a dot."""
    return str(ticker or "").strip().upper().replace(".", "-")


# ── last known value ─────────────────────────────────────────────────────────

def remember(ticker: str, value, source: str) -> None:
    """Keep a market cap we actually obtained, for when no source can answer."""
    v = _valid(value)
    sym = _norm(ticker)
    if v is None or not sym:
        return
    try:
        ttl = max(1, int(float(config.MARKET_CAP_CACHE_HOURS) * 3600))
        cache.set(f"mcap:{sym}", {"value": v, "source": str(source), "ts": time.time()}, ttl=ttl)
    except Exception as exc:
        log.debug(f"{sym}: market-cap cache write failed ({exc})")


def cached(ticker: str) -> Optional[KnownCap]:
    """The last market cap remembered for ``ticker`` within the cache window."""
    try:
        hit = cache.get(f"mcap:{_norm(ticker)}")
        if not isinstance(hit, dict):
            return None
        v = _valid(hit.get("value"))
        if v is None:
            return None
        age = max(0.0, (time.time() - float(hit.get("ts") or 0)) / 3600)
        return KnownCap(v, str(hit.get("source") or "unknown"), round(age, 1))
    except Exception:
        return None


# ── SEC EDGAR ────────────────────────────────────────────────────────────────

def _sec_get(url: str):
    """GET a SEC JSON document. None on 404, error, or during backoff."""
    global _last_request, _retry_after
    with _lock:
        now = time.monotonic()
        if now < _retry_after:
            return None
        wait = _MIN_INTERVAL_SEC - (now - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    try:
        resp = get_retry_session().get(
            url,
            headers={"User-Agent": config.SEC_USER_AGENT, "Accept": "application/json"},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        # A 403 here usually means SEC wants a contact in the User-Agent.
        with _lock:
            _retry_after = time.monotonic() + _FAILURE_BACKOFF_SEC
        log.warning(f"SEC EDGAR request failed ({type(exc).__name__}: {str(exc)[:120]}) — "
                    f"pausing SEC lookups for {_FAILURE_BACKOFF_SEC / 60:.0f} min "
                    "(set SEC_USER_AGENT to 'Name contact@email' if this is a 403)")
        return None


def _cik_map() -> dict:
    hit = cache.get(_CIK_MAP_KEY)
    if isinstance(hit, dict) and hit:
        return hit
    payload = _sec_get(_SEC_TICKERS_URL)
    rows = payload.values() if isinstance(payload, dict) else []
    mapping = {}
    for row in rows:
        try:
            mapping[_norm(row["ticker"])] = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
    if mapping:
        cache.set(_CIK_MAP_KEY, mapping, ttl=_CIK_MAP_TTL)
    return mapping


def _latest_shares(payload) -> Optional[float]:
    """The most recent share count in a companyconcept payload.

    Latest period end wins; at the same end, the shortest period (a quarter over
    a year-to-date average), then the latest filing (an amendment over the
    original).
    """
    try:
        facts = payload["units"]["shares"]
    except (KeyError, TypeError):
        return None
    best_key, best_val = None, None
    for f in facts if isinstance(facts, list) else []:
        val = _valid(f.get("val")) if isinstance(f, dict) else None
        end = f.get("end") if isinstance(f, dict) else None
        if val is None or not end:
            continue
        start = f.get("start") or end
        # ISO dates compare correctly as strings; a later start = a shorter period.
        key = (str(end), str(start), str(f.get("filed") or ""))
        if best_key is None or key > best_key:
            best_key, best_val = key, val
    return best_val


def _sec_shares(ticker: str) -> Optional[tuple[float, str]]:
    sym = _norm(ticker)
    ckey = f"sec:shares:{sym}"
    hit = cache.get(ckey)
    if isinstance(hit, dict):
        v = _valid(hit.get("shares"))
        return (v, str(hit.get("concept"))) if v else None
    cik = _cik_map().get(sym)
    if cik is None:
        return None
    for taxonomy, tag in _SHARE_CONCEPTS:
        shares = _latest_shares(_sec_get(_SEC_CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)))
        if shares:
            cache.set(ckey, {"shares": shares, "concept": tag}, ttl=_SHARES_TTL)
            return shares, tag
    if time.monotonic() >= _retry_after:      # a genuine "no data", not a backoff
        cache.set(ckey, {"shares": None}, ttl=_NO_SHARES_TTL)
    return None


def sec_market_cap(ticker: str, price) -> Optional[float]:
    """Shares outstanding (SEC EDGAR) × ``price``, or None. Never raises."""
    try:
        if not config.SEC_MARKET_CAP_ENABLED:
            return None
        p = _valid(price)
        if p is None:
            return None
        found = _sec_shares(ticker)
        if found is None:
            return None
        shares, concept = found
        value = shares * p
        log.info(f"{_norm(ticker)}: market cap ≈ ${value / 1e9:,.2f}B from SEC {concept} × ${p:,.2f}")
        return value
    except Exception as exc:
        log.debug(f"{ticker}: SEC market-cap estimate failed ({exc})")
        return None
