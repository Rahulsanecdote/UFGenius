"""Is a symbol common stock, or a warrant / right / unit?

The movers lists do not distinguish. Below $1 they are mostly NOT stocks:
measured 2026-09-30 at 09:47 ET with the discovery price floor removed, 34
sub-$1 candidates came back, and of the lowest 25 only three were common stock
(RETO, FNGR, NIVF) — the rest were warrants (ASTLW, DAICW, NIVFW, ...), rights
(ASGI.RT, AACPR) and warrant series (OPENZ, TGE.WS, SKYH.WS). A warrant at
$0.004 moving -59% scores the discovery maximum, so with no filter 19 of the
40 watch slots went to sub-$1 names, pushing real movers out to make room for
derivatives of them — NIVFW rode in beside NIVF.

**The asset NAME is the signal, not the symbol.** Alpaca names every one of
these plainly — "NewGenIvf Group Limited Warrants", "Apogee Acquisition Corp
Rights", "Opendoor Technologies Inc Series Z Warrants". A suffix rule would have
missed OPENZ (the Z series is not W/R/U) and, applied loosely, would flag SNOW,
which is Snowflake. So the primary path reads the name from Alpaca's asset list,
fetched once and disk-cached, and the suffix rule is only a fallback for symbols
that list does not contain, or when it cannot be fetched at all.

Which path answered is disclosed via `last_fetch_ok()`, because "excluded 12
derivatives by name" and "excluded 12 by guessing from the ticker" are different
claims. Never raises.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional

from src.data import cache
from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

__all__ = ["is_derivative", "name_is_derivative", "last_fetch_ok"]

# Word-bounded, so "United Airlines" is not a unit and "Warrantech" is not a
# warrant. Matched against the issuer's full security name.
_WARRANT_OR_RIGHT_RE = re.compile(r"\b(warrants?|rights?)\b", re.IGNORECASE)
_UNIT_RE = re.compile(r"\bunits?\b", re.IGNORECASE)
# ...but a UNIT is only a derivative when it is a SPAC-style bundle ("Acquisition
# Corp Unit 1 CL A & 1/3 WT"). A partnership's or trust's units ARE its equity:
# Energy Transfer is "Energy Transfer LP Common Units representing limited
# partner interests", and Plains All American, Alliance Resource, CrossAmerica,
# UNG and the Grayscale trusts read the same way. The first cut of this rule
# flagged all of them — an audit of every name in the 14,389-asset list caught
# it before it shipped. Silently dropping ET and PAA from discovery is exactly
# the invisible-absence failure the whole movers layer is built against.
_EQUITY_UNIT_RE = re.compile(
    r"common\s+units?|units?\s+(representing|repstg|repr)|limited\s+partner"
    r"|beneficial|preferred|\bl\.?\s?p\.?(?=\W|$)|\bfund\b|\btrust\b",
    re.IGNORECASE,
)


def name_is_derivative(name: str) -> bool:
    """Classify one security by its full name. Pure; never raises."""
    try:
        n = str(name or "")
        if _WARRANT_OR_RIGHT_RE.search(n):
            return True
        return bool(_UNIT_RE.search(n)) and not _EQUITY_UNIT_RE.search(n)
    except Exception:
        return False

# Fallback only. Dotted/slashed class suffixes (NYSE/AMEX: .WS .WT .RT .U) and
# the Nasdaq fifth-character convention (W warrant, R right, U unit) — which is
# exactly the rule the name check exists to replace, so it only runs when the
# name is unavailable.
_SUFFIX_RE = re.compile(r"([./](WS|WT|RT|U|W|R)$)|(^[A-Z]{4}[WRU]$)")

_CACHE_KEY = "security_type:alpaca_assets"
_TTL_SEC = 12 * 3600          # the listed universe changes daily at most
_FAILURE_BACKOFF_SEC = 300.0  # same as halts.py: never re-pay a dead fetch every cycle

_lock = threading.Lock()
_retry_after = 0.0
_last_fetch_ok = False


def _fetch_map() -> Optional[dict[str, bool]]:
    """{symbol: is_derivative} for every active US equity asset, or None."""
    global _retry_after, _last_fetch_ok
    # Credentials first, cache second: without keys this can never refresh, and
    # checking here means a keyless caller — every unit test, by conftest's
    # isolation fixture — never reads whatever map a live run left in data/.
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        _last_fetch_ok = False
        return None
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        _last_fetch_ok = True
        return cached
    with _lock:
        cached = cache.get(_CACHE_KEY)        # another thread may have filled it
        if cached is not None:
            _last_fetch_ok = True
            return cached
        if time.monotonic() < _retry_after:
            return None
        try:
            from src.data.fetcher import _alpaca_headers, _alpaca_trading_base_url
            resp = get_retry_session().get(
                f"{_alpaca_trading_base_url()}/v2/assets",
                headers=_alpaca_headers(),
                params={"status": "active", "asset_class": "us_equity"},
                timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC,
                         max(config.REQUEST_TIMEOUT_SEC, 30)),
            )
            resp.raise_for_status()
            rows = resp.json()
            mapping = {
                str(a.get("symbol", "")).upper(): name_is_derivative(a.get("name"))
                for a in rows if a.get("symbol")
            }
            if not mapping:
                raise ValueError("empty asset list")
        except Exception as exc:
            _last_fetch_ok = False
            _retry_after = time.monotonic() + _FAILURE_BACKOFF_SEC
            log.debug(f"security-type: asset list fetch failed ({type(exc).__name__}) — "
                      f"falling back to symbol suffixes for {_FAILURE_BACKOFF_SEC:.0f}s")
            return None
        _last_fetch_ok = True
        _retry_after = 0.0
        cache.set(_CACHE_KEY, mapping, ttl=_TTL_SEC)
        log.info(f"security-type: {len(mapping)} assets, "
                 f"{sum(mapping.values())} warrants/rights/units")
        return mapping


def is_derivative(symbol: str) -> bool:
    """True for a warrant, right or unit; False for common stock or unknown.

    Unknown leans False on purpose: excluding a real stock hides it with
    nothing to point at, while admitting a stray derivative costs one visible
    line.
    """
    try:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False
        mapping = _fetch_map()
        if mapping is not None and sym in mapping:
            return mapping[sym]
        return bool(_SUFFIX_RE.search(sym))
    except Exception:
        return False


def last_fetch_ok() -> bool:
    """Did the most recent asset-list lookup actually have data?"""
    return _last_fetch_ok
