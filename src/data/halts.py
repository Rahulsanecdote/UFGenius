"""Trade-halt awareness — which symbols are halted right now, and why.

A halted stock is the one thing a mover list must not treat as a normal
candidate: you cannot act on it, and LULD reopens gap violently. Worse, a halt
poisons the *monitor* — no trades print while halted, so relative volume
collapses and the invalidation rules read a fake "the move is fading" on a
setup that never moved at all.

Source: Nasdaq Trader's public UTP trade-halt feed, the official dissemination
channel for LULD volatility pauses (``LUDP``), news halts (``T1``), and the
rest. It is free, needs no key, and covers every US venue — not just Nasdaq.

Parsing is deliberately **content-addressed**: fields are located by their
local tag name with any XML namespace stripped, so a namespace or ordering
change degrades to "no data" rather than to wrong data (the discipline audit
M10 imposed on ``data/universe.py``).

**Fails open, on purpose.** Any fetch or parse problem yields an empty map,
i.e. "no halts known" — so an outage of this feed can never silently mute
every alert. Callers that need to distinguish "nothing halted" from "we could
not check" read ``last_fetch_ok()``.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from src.data import cache
from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

_FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
_CACHE_KEY = "trade_halts:nasdaq"
_EASTERN = ZoneInfo("America/New_York")

# Reason codes worth naming in a UI; anything else is passed through as-is.
_REASON_TEXT = {
    "LUDP": "LULD volatility pause",
    "LUDS": "LULD pause — straddle state",
    "T1": "news pending",
    "T2": "news released",
    "T3": "news and resumption times",
    "T12": "additional information requested",
    "H10": "SEC trading suspension",
    "H4": "non-compliance",
    "D": "delisted",
    "M": "volatility trading pause",
    "IPO1": "IPO not yet traded",
}

# Set by the last fetch attempt so callers can tell "nothing halted" from
# "the feed did not answer" — the two are very different for alerting.
_last_fetch_ok = False

# A dead feed must not cost a connect-timeout-plus-retries on every worker
# cycle, so a failure parks further attempts for this long. Monotonic deadline.
_FAILURE_BACKOFF_SEC = 300.0
_retry_after = 0.0


@dataclass
class HaltRecord:
    """One halt as published by the UTP feed."""

    symbol: str
    reason_code: str = ""
    halted_at: Optional[datetime] = None    # ET-aware
    resumes_at: Optional[datetime] = None   # ET-aware; None = not yet scheduled
    market: str = ""
    name: str = ""

    @property
    def reason(self) -> str:
        """Human-readable reason, falling back to the raw code."""
        return _REASON_TEXT.get(self.reason_code.upper(), self.reason_code or "halted")

    def is_active(self, now_et: datetime) -> bool:
        """Still halted as of ``now_et``?

        A record with no resumption time is an open halt. One whose resumption
        has already passed is history — the feed keeps the day's records, so
        this filter is what makes the map "currently halted" rather than
        "halted at some point today".
        """
        if self.halted_at is not None and self.halted_at > now_et:
            return False   # scheduled/erroneous future record
        if self.resumes_at is None:
            return True
        return self.resumes_at > now_et


def _localname(tag: str) -> str:
    """Tag without its XML namespace, lowercased."""
    return tag.rsplit("}", 1)[-1].strip().lower()


def _parse_et(date_text: str, time_text: str) -> Optional[datetime]:
    """Combine the feed's separate ET date + time fields into one datetime."""
    date_text, time_text = (date_text or "").strip(), (time_text or "").strip()
    if not date_text or not time_text:
        return None
    for date_fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        for time_fmt in ("%H:%M:%S", "%H:%M"):
            try:
                dt = datetime.strptime(f"{date_text} {time_text}", f"{date_fmt} {time_fmt}")
                return dt.replace(tzinfo=_EASTERN)
            except ValueError:
                continue
    return None


def parse_halt_feed(xml_text: str) -> list[HaltRecord]:
    """Parse the UTP halt feed into records. Returns [] on anything unexpected."""
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        log.debug(f"halts: feed parse failed ({type(exc).__name__})")
        return []

    records: list[HaltRecord] = []
    for item in root.iter():
        if _localname(item.tag) != "item":
            continue
        # Collect the item's children by local name — position-independent.
        fields = {_localname(child.tag): (child.text or "").strip()
                  for child in item}
        symbol = (fields.get("issuesymbol") or "").upper().strip()
        if not symbol:
            continue
        records.append(HaltRecord(
            symbol=symbol,
            reason_code=fields.get("reasoncode", ""),
            halted_at=_parse_et(fields.get("haltdate", ""), fields.get("halttime", "")),
            resumes_at=_parse_et(
                fields.get("resumptiondate", ""),
                # Trading resumes at the trade time; the quote time is earlier.
                fields.get("resumptiontradetime", "")
                or fields.get("resumptionquotetime", ""),
            ),
            market=fields.get("market", ""),
            name=fields.get("issuename", ""),
        ))
    return records


def _fetch_feed() -> list[HaltRecord]:
    """Fetch + parse the feed, TTL-cached. [] on any failure (never raises)."""
    global _last_fetch_ok, _retry_after
    ttl = max(5, int(config.MOVERS_HALT_CACHE_TTL_SEC))
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        _last_fetch_ok = True
        # Timestamps round-trip through the cache as ISO strings.
        return [_rehydrate(row) for row in cached]
    if time.monotonic() < _retry_after:
        # Still inside the post-failure backoff — stay quiet rather than
        # re-paying the timeout on every scan cycle.
        return []
    try:
        resp = get_retry_session().get(
            _FEED_URL,
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        records = parse_halt_feed(resp.text)
    except Exception as exc:
        _last_fetch_ok = False
        _retry_after = time.monotonic() + _FAILURE_BACKOFF_SEC
        log.debug(f"halts: feed fetch failed ({type(exc).__name__}) — "
                  f"backing off {_FAILURE_BACKOFF_SEC:.0f}s")
        return []
    _last_fetch_ok = True
    _retry_after = 0.0
    cache.set(
        _CACHE_KEY,
        [{"symbol": r.symbol, "reason_code": r.reason_code,
          "halted_at": r.halted_at.isoformat() if r.halted_at else None,
          "resumes_at": r.resumes_at.isoformat() if r.resumes_at else None,
          "market": r.market, "name": r.name} for r in records],
        ttl=ttl,
    )
    return records


def _rehydrate(row: dict) -> HaltRecord:
    """Rebuild a record from its cached (JSON-safe) form."""
    def _dt(value):
        return datetime.fromisoformat(value) if value else None
    return HaltRecord(
        symbol=row["symbol"], reason_code=row.get("reason_code", ""),
        halted_at=_dt(row.get("halted_at")), resumes_at=_dt(row.get("resumes_at")),
        market=row.get("market", ""), name=row.get("name", ""),
    )


def last_fetch_ok() -> bool:
    """Did the most recent halt lookup actually reach the feed?

    ``False`` means an empty halt map is "unknown", not "nothing is halted".
    """
    return _last_fetch_ok


def active_halts(now: Optional[datetime] = None) -> dict[str, HaltRecord]:
    """Currently-halted symbols → their halt record.

    Empty when the guard is disabled or the feed is unreachable (fail-open).
    """
    if not config.MOVERS_HALTS_ENABLED:
        return {}
    try:
        now_et = (now or datetime.now(_EASTERN)).astimezone(_EASTERN)
        out: dict[str, HaltRecord] = {}
        for record in _fetch_feed():
            if record.is_active(now_et):
                # Keep the most recent halt when a symbol appears twice.
                prior = out.get(record.symbol)
                if prior is None or (
                    record.halted_at and prior.halted_at
                    and record.halted_at > prior.halted_at
                ):
                    out[record.symbol] = record
        return out
    except Exception as exc:  # halt lookup must never break a scan
        log.debug(f"halts: active_halts failed ({type(exc).__name__})")
        return {}
