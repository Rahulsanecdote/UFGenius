"""Movers-discovery providers — a fallback chain, not a single point of failure.

Discovery used to be FMP-only, so an exhausted daily quota took the whole
intraday discovery path down (observed 2026-08-17: the list went empty mid-
session while the market was plainly not quiet). This module turns the source
of a mover list into a chain — the first provider that actually answers wins,
and the next one covers when it doesn't.

Each adapter normalises to the shape the merge step already expects
(``symbol`` / ``price`` / ``changesPercentage`` / ``name``) and returns:

* ``list``  — the provider answered. An empty list is a real answer: the
  market was quiet, which is NOT the same as a failure and must not fall
  through to the next provider as though the source were broken.
* ``None``  — the provider could not answer (no key, HTTP error, or a payload
  that isn't what the API documents). The chain moves on and the failure is
  recorded.

That two-value contract is the whole point: FMP replies to an exhausted quota
with **HTTP 200 and a JSON object**, so "looks successful but isn't" has to be
detectable, or a dead key silently reads as an empty market.

Providers differ in what they can serve — Alpaca's screener has no price on its
most-actives rows, and Polygon's snapshot has no most-actives endpoint at all —
so each declares the sources it supports and is skipped for the others rather
than reporting a false failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

GAINERS, LOSERS, MOST_ACTIVES = "gainers", "losers", "most_actives"

_FMP_URL = "https://financialmodelingprep.com/stable/{endpoint}"
_FMP_ENDPOINTS = {
    GAINERS: "biggest-gainers",
    LOSERS: "biggest-losers",
    MOST_ACTIVES: "most-actives",
}
_ALPACA_SCREENER_URL = (
    config.env("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    + "/v1beta1/screener/stocks/{endpoint}"
)
_POLYGON_URL = (
    "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}"
)

_TOP_N = 50


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _row(symbol, price, change_pct, name="") -> Optional[dict]:
    """Normalised row, or None when the essentials are missing."""
    symbol = str(symbol or "").upper().strip()
    price, change_pct = _num(price), _num(change_pct)
    if not symbol or price is None or change_pct is None:
        return None
    return {"symbol": symbol, "price": price,
            "changesPercentage": change_pct, "name": str(name or "")}


def _normalise(items: list, to_row: Callable[[dict], Optional[dict]]
               ) -> Optional[list[dict]]:
    """Map raw rows, distinguishing "empty" from "unusable".

    A payload that carried rows but yielded none we can read is a schema or
    entitlement change, i.e. the provider could NOT answer — returning `[]`
    there would read as a quiet market and stop the chain, so the fallback
    would never run (Codex P2). An actually-empty payload stays `[]`.
    """
    rows = [row for row in (to_row(it) for it in items if isinstance(it, dict))
            if row is not None]
    if items and not rows:
        log.warning("movers: provider returned %d row(s) but none were usable "
                    "— treating as a failure so the chain falls through", len(items))
        return None
    return rows


# ── FMP (the incumbent) ──────────────────────────────────────────────────────

def fetch_fmp(source: str) -> Optional[list[dict]]:
    endpoint = _FMP_ENDPOINTS.get(source)
    if endpoint is None or not config.FMP_KEY:
        return None
    try:
        resp = get_retry_session().get(
            _FMP_URL.format(endpoint=endpoint),
            params={"apikey": config.FMP_KEY},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug(f"movers/fmp {endpoint}: {type(exc).__name__}")
        return None
    if not isinstance(data, list):
        # An exhausted quota arrives as HTTP 200 + {"Error Message": ...}.
        log.warning(f"movers/fmp {endpoint}: non-list payload "
                    f"({type(data).__name__}) — treating as a failure")
        return None
    return _normalise(data, lambda it: _row(
        it.get("symbol"), it.get("price"),
        it.get("changesPercentage", it.get("changePercentage")), it.get("name")))


# ── Alpaca screener (keys already present for the broker/stream) ─────────────

def _fetch_alpaca_json(endpoint: str, params: dict) -> Optional[dict]:
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        return None
    try:
        resp = get_retry_session().get(
            _ALPACA_SCREENER_URL.format(endpoint=endpoint),
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            params=params,
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # A plan without the screener answers 403 here; that is a normal
        # "cannot serve", and the chain simply moves on.
        log.debug(f"movers/alpaca {endpoint}: {type(exc).__name__}")
        return None
    return data if isinstance(data, dict) else None


def fetch_alpaca(source: str) -> Optional[list[dict]]:
    """Alpaca's screener. Serves gainers/losers only.

    Its most-actives rows carry volume and trade count but no price or percent
    change, so there is nothing to build a candidate from — that source is
    declared unsupported rather than returned empty.
    """
    if source not in (GAINERS, LOSERS):
        return None
    data = _fetch_alpaca_json("movers", {"top": _TOP_N})
    if data is None:
        return None
    key = "gainers" if source == GAINERS else "losers"
    items = data.get(key)
    if not isinstance(items, list):
        return None
    return _normalise(items, lambda it: _row(
        it.get("symbol"), it.get("price"), it.get("percent_change")))


# ── Polygon snapshot ─────────────────────────────────────────────────────────

def fetch_polygon(source: str) -> Optional[list[dict]]:
    """Polygon's snapshot gainers/losers. No most-actives equivalent exists."""
    if source not in (GAINERS, LOSERS) or not config.POLYGON_KEY:
        return None
    direction = "gainers" if source == GAINERS else "losers"
    try:
        resp = get_retry_session().get(
            _POLYGON_URL.format(direction=direction),
            params={"apiKey": config.POLYGON_KEY},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug(f"movers/polygon {direction}: {type(exc).__name__}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), list):
        return None
    def _to_row(it: dict):
        day = it.get("day") if isinstance(it.get("day"), dict) else {}
        last = it.get("lastTrade") if isinstance(it.get("lastTrade"), dict) else {}
        # Prefer the last trade; fall back to the day bar's close.
        price = last.get("p") if last.get("p") is not None else day.get("c")
        return _row(it.get("ticker"), price, it.get("todaysChangePerc"))

    return _normalise(data["tickers"], _to_row)


@dataclass(frozen=True)
class MoversProvider:
    name: str
    fetch: Callable[[str], Optional[list[dict]]]
    supports: frozenset = field(default_factory=frozenset)
    # Whether this provider has what it needs to be tried at all. Keeping this
    # separate from `fetch` lets the chain say "nothing was configured" rather
    # than "everything failed" — very different things to debug.
    configured: Callable[[], bool] = lambda: True


_PROVIDERS = {
    "alpaca": MoversProvider(
        "alpaca", fetch_alpaca, frozenset({GAINERS, LOSERS}),
        lambda: bool(config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)),
    "polygon": MoversProvider(
        "polygon", fetch_polygon, frozenset({GAINERS, LOSERS}),
        lambda: bool(config.POLYGON_KEY)),
    "fmp": MoversProvider(
        "fmp", fetch_fmp, frozenset({GAINERS, LOSERS, MOST_ACTIVES}),
        lambda: bool(config.FMP_KEY)),
}


def provider_chain(names: Optional[list[str]] = None) -> list[MoversProvider]:
    """The configured chain, in order. Unknown names are logged and skipped."""
    names = names if names is not None else list(config.MOVERS_PROVIDERS)
    chain = []
    for name in names:
        provider = _PROVIDERS.get(str(name).strip().lower())
        if provider is None:
            log.warning(f"movers: unknown provider '{name}' — skipping")
            continue
        chain.append(provider)
    return chain
