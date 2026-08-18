"""Pre-market movers discovery — the universe the pre-market screener needs.

The existing movers chain (``movers_providers.py``) answers "what moved in the
**regular session**". Before 09:30 that is *yesterday's* session: on 2026-08-18
at 09:02 ET the movers panel listed SIC/WETO/IPST at their 2026-08-17 closing
prices, and none of those names had an extended-hours print that morning. Feeding
that list to the pre-market screener produced 6 usable snapshots out of 50 — not
a bug in the screener, a universe describing the wrong session.

So `universe=MOVERS` and the pre-market screener never overlap usefully: during
04:00–09:30, when the screener works, the movers universe is stale; once it
refreshes at the open, the pre-market window has closed. This module is the
missing half — discovery of what is moving **right now, in extended hours**.

Coverage is the honest problem here, and it is disclosed rather than hidden.
Providers differ in what they can even see:

* ``market_wide``   — the provider scans every US ticker, so a name gapping on an
  08:00 press release is visible even if nothing yesterday would have flagged it.
* ``bounded_pool``  — the provider can only rank a **candidate pool** assembled
  from prior-session lists. A fresh gapper that was quiet yesterday is invisible,
  and no amount of ranking fixes that.

Every result carries which provider served it and which of those two it was, for
the same reason ``served_by`` exists on the regular-session chain: a fallback
changes the *character* of the answer, and silently swapping breadth for
convenience is how "we scanned the market" becomes a lie.

Discovery only. Nothing here sizes, gates, or places an order; it produces a
ticker list for the screener, which is itself firewalled from the money path.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

COVERAGE_MARKET_WIDE = "market_wide"
COVERAGE_BOUNDED_POOL = "bounded_pool"

_POLYGON_SNAPSHOT_URL = (
    "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
)
_YAHOO_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
)
_YAHOO_HEADERS = {"User-Agent": config.CONSTITUENT_FETCH_USER_AGENT}

# A minute bar older than this is not "now" — during a quiet pre-market a stale
# aggregate would otherwise be read as a live quote.
_MAX_BAR_AGE_SEC = 900.0

_EASTERN = ZoneInfo("America/New_York")
_PM_OPEN, _PM_CLOSE = dtime(4, 0), dtime(9, 30)


def in_premarket_session(now: Optional[datetime] = None) -> bool:
    """True during 04:00–09:30 ET on a weekday.

    Outside it an empty result is the *correct* answer, not a fault — but the two
    are indistinguishable from the count alone, so callers use this to say which
    one they are reporting. Holidays are not modelled: an empty list on a market
    holiday reads as "nothing is moving", which is true.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(_EASTERN)
    return now.weekday() < 5 and _PM_OPEN <= now.time() < _PM_CLOSE


def _num(value) -> Optional[float]:
    """float() that refuses NaN/inf — they defeat every downstream comparison."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class PremarketMover:
    """One extended-hours mover. ``change_pct`` is measured vs the prior close."""

    ticker: str
    price: float
    change_pct: float
    prev_close: float
    volume: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "change_pct": round(self.change_pct, 2),
            "prev_close": round(self.prev_close, 4),
            "volume": None if self.volume is None else int(self.volume),
        }


def _mover(ticker, price, prev_close, volume=None) -> Optional[PremarketMover]:
    """Build a mover from a live price + prior close, or None if unusable."""
    ticker = str(ticker or "").upper().strip()
    price, prev_close = _num(price), _num(prev_close)
    if not ticker or price is None or prev_close is None or prev_close <= 0 or price <= 0:
        return None
    return PremarketMover(
        ticker=ticker,
        price=price,
        change_pct=(price - prev_close) / prev_close * 100.0,
        prev_close=prev_close,
        volume=_num(volume),
    )


# ── Polygon: the only market-wide option ─────────────────────────────────────

def fetch_polygon(now: Optional[datetime] = None) -> Optional[list[PremarketMover]]:
    """Full-market snapshot, ranked from the most recent minute bar.

    Polygon's ``/snapshot/.../tickers`` covers every US ticker and its ``min``
    aggregate spans extended hours, so ``min.c`` vs ``prevDay.c`` is the actual
    pre-market change — and it is genuinely market-wide.

    Returns ``None`` (not ``[]``) whenever the provider could not answer, so the
    chain falls through instead of reporting a quiet pre-market: the snapshot is
    a paid entitlement, and a plan without it answers 403 rather than empty.
    """
    if not config.POLYGON_KEY:
        return None
    try:
        resp = get_retry_session().get(
            _POLYGON_SNAPSHOT_URL,
            params={"apiKey": config.POLYGON_KEY},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug(f"premarket-movers/polygon: {type(exc).__name__}")
        return None
    tickers = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(tickers, list):
        return None

    now = now or datetime.now(timezone.utc)
    cutoff_ns = (now - timedelta(seconds=_MAX_BAR_AGE_SEC)).timestamp() * 1e9
    out: list[PremarketMover] = []
    for row in tickers:
        if not isinstance(row, dict):
            continue
        minute = row.get("min") if isinstance(row.get("min"), dict) else {}
        prev = row.get("prevDay") if isinstance(row.get("prevDay"), dict) else {}
        # A stale minute bar is last night's, not this morning's. Rows with no
        # timestamp are dropped rather than trusted — during pre-market most
        # tickers simply have not traded, and that is the answer, not a gap.
        bar_ts = _num(minute.get("t"))
        if bar_ts is None or bar_ts < cutoff_ns:
            continue
        mover = _mover(row.get("ticker"), minute.get("c"), prev.get("c"),
                       minute.get("av") or minute.get("v"))
        if mover is not None:
            out.append(mover)
    if not out and tickers:
        # A full snapshot that yielded nothing usable is a schema/entitlement
        # problem, not a quiet market — fall through rather than assert silence.
        log.warning("premarket-movers/polygon: %d snapshot rows, none usable "
                    "— treating as a failure", len(tickers))
        return None
    return out


# ── Yahoo: keyless, but only ever a bounded pool ─────────────────────────────

def _yahoo_pool(screeners: list[str]) -> Optional[list[dict]]:
    """Assemble the candidate pool from Yahoo's predefined screeners."""
    session = get_retry_session()
    pool: dict[str, dict] = {}
    answered = False
    for name in screeners:
        try:
            resp = session.get(
                _YAHOO_SCREENER_URL,
                params={"scrIds": str(name), "count": 100},
                headers=_YAHOO_HEADERS,
                timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
            )
            resp.raise_for_status()
            results = resp.json()["finance"]["result"]
        except Exception as exc:
            log.debug(f"premarket-movers/yahoo {name}: {type(exc).__name__}")
            continue
        if not isinstance(results, list) or not results:
            continue
        answered = True
        for quote in results[0].get("quotes") or []:
            if isinstance(quote, dict) and quote.get("symbol"):
                pool.setdefault(str(quote["symbol"]).upper(), quote)
    # Not one screener answered → the provider could not answer at all.
    return list(pool.values()) if answered else None


def fetch_yahoo(now: Optional[datetime] = None) -> Optional[list[PremarketMover]]:
    """Rank Yahoo's prior-session screener pool by its pre-market print.

    Keyless, so this is the provider that always exists — but the pool comes
    from *yesterday's* gainers/losers/actives, which is precisely the blind spot
    this module was written to expose. It is a real answer for names that were
    already on the radar and no answer at all for a fresh 08:00 gapper, hence
    ``COVERAGE_BOUNDED_POOL``. Kept last in the chain for that reason.
    """
    quotes = _yahoo_pool(list(config.PREMARKET_MOVERS_YAHOO_SCREENERS))
    if quotes is None:
        return None
    out: list[PremarketMover] = []
    for quote in quotes:
        pre_price = quote.get("preMarketPrice")
        pre_chg = _num(quote.get("preMarketChangePercent"))
        if pre_price is None or pre_chg is None:
            continue
        price = _num(pre_price)
        if price is None or price <= 0:
            continue
        # Yahoo states the pre-market change against the last regular close;
        # derive that close rather than reusing regularMarketPreviousClose,
        # which is the close BEFORE the last session and would misprice every
        # name that moved yesterday.
        denom = 1.0 + (pre_chg / 100.0)
        prev_close = price / denom if denom > 0 else None
        mover = _mover(quote.get("symbol"), price, prev_close,
                       quote.get("preMarketVolume"))
        if mover is not None:
            out.append(mover)
    return out


@dataclass(frozen=True)
class PremarketProvider:
    name: str
    fetch: Callable[..., Optional[list[PremarketMover]]]
    coverage: str
    configured: Callable[[], bool] = lambda: True
    # Human-readable statement of what this provider can and cannot see. It ends
    # up in the screener's disclosures, where the operator reads it.
    note: str = ""


_PROVIDERS = {
    "polygon": PremarketProvider(
        "polygon", fetch_polygon, COVERAGE_MARKET_WIDE,
        lambda: bool(config.POLYGON_KEY),
        "Full-market snapshot: every US ticker with an extended-hours print is "
        "eligible, including names with no prior-session activity."),
    "yahoo": PremarketProvider(
        "yahoo", fetch_yahoo, COVERAGE_BOUNDED_POOL, lambda: True,
        "Ranks a candidate pool built from the PRIOR session's gainers/losers/"
        "actives. A stock gapping on fresh news that was quiet yesterday is NOT "
        "in the pool and cannot appear here at any rank."),
}

# Per-thread, like the movers chain: the dashboard runs the worker in a daemon
# thread inside the same gunicorn process that serves requests, so process-wide
# state would let one run read another's result.
_state = threading.local()


def _info() -> dict:
    info = getattr(_state, "info", None)
    if info is None:
        info = {"served_by": None, "coverage": None, "pool_size": 0,
                "attempted": [], "reason": None}
        _state.info = info
    return info


def last_discovery_info() -> dict:
    """Provenance of the most recent :func:`get_premarket_universe` in this thread.

    ``coverage`` is the field that matters: it says whether the list could have
    contained a fresh gapper at all.
    """
    info = _info()
    return {k: (list(v) if isinstance(v, list) else v) for k, v in info.items()}


def provider_chain(names: Optional[list[str]] = None) -> list[PremarketProvider]:
    """The configured chain, in order. Unknown names are logged and skipped."""
    names = names if names is not None else list(config.PREMARKET_MOVERS_PROVIDERS)
    chain = []
    for name in names:
        provider = _PROVIDERS.get(str(name).strip().lower())
        if provider is None:
            log.warning(f"premarket-movers: unknown provider '{name}' — skipping")
            continue
        chain.append(provider)
    return chain


def fetch_premarket_movers(
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[PremarketMover]:
    """Movers in the current extended-hours session, ranked by |change|.

    Both directions: the screener gates on ``abs(gap_pct)``, so a −40% gap is as
    much a candidate as a +40% one. The first provider that answers serves; its
    identity and coverage class land in :func:`last_discovery_info`.
    """
    info = _info()
    in_session = in_premarket_session(now)
    info.update({"served_by": None, "coverage": None, "pool_size": 0,
                 "attempted": [], "reason": None, "in_session": in_session})

    min_price = float(config.PREMARKET_MOVERS_MIN_PRICE)
    min_change = float(config.PREMARKET_MOVERS_MIN_CHANGE_PCT)
    cap = int(limit if limit is not None else config.PREMARKET_MOVERS_LIMIT)

    tried = []
    for provider in provider_chain():
        try:
            if not provider.configured():
                continue
        except Exception:
            continue
        tried.append(provider.name)
        info["attempted"] = list(tried)
        try:
            movers = provider.fetch(now=now)
        except Exception as exc:
            log.debug(f"premarket-movers/{provider.name}: {type(exc).__name__}")
            movers = None
        if movers is None:
            continue
        info["pool_size"] = len(movers)
        kept = [m for m in movers
                if m.price >= min_price and abs(m.change_pct) >= min_change]
        kept.sort(key=lambda m: (-abs(m.change_pct), m.ticker))
        note = provider.note
        if not kept and not in_session:
            # Zero outside 04:00–09:30 ET is the session being closed, not a
            # quiet tape or a broken provider — three states one count cannot
            # distinguish, so the one we know gets said.
            note = ("Outside the 04:00–09:30 ET pre-market session — an empty "
                    "result is expected. " + note)
        info.update({"served_by": provider.name, "coverage": provider.coverage,
                     "reason": note})
        log.info(f"premarket movers: {len(kept)} of {len(movers)} cleared "
                 f"(>= {min_change}% and >= ${min_price}) via {provider.name} "
                 f"[{provider.coverage}]")
        return kept[:cap]

    info["reason"] = ("no_provider_answered" if tried else "no_provider_configured")
    log.warning(f"premarket movers: {info['reason']}")
    return []


def get_premarket_universe(limit: Optional[int] = None) -> list[str]:
    """Ticker list for the pre-market screener. Empty on any failure (fail-soft)."""
    return [m.ticker for m in fetch_premarket_movers(limit=limit)]
