"""Finviz provider — fundamentals snapshot + screener.

Finviz publishes no free API, so this reads the public HTML pages. Two
consequences shape the whole module:

1. **It is opt-in and polite.** Disabled unless `finviz.enabled` is set. Requests
   are serialised behind a minimum interval, routed through `src/utils/http.py`
   (timeout + bounded retry), and cached on disk, so a scan costs one request per
   ticker per TTL rather than one per call. Finviz's terms restrict automated
   access and heavy use expects an Elite subscription — that is the operator's
   call to make, which is why nothing here turns itself on.
2. **It fails soft, never hard.** Scraped markup breaks when a site restyles, so
   every entry point returns `None`/`[]` on any failure and the caller keeps its
   existing behaviour. This is a *supplementary* fundamentals source; it never
   sits on the money path.

Tables are located by their **content** (a header/label that must be present),
never by position — the same discipline audit M10 applied to `universe.py`, so a
layout change degrades to "no data" instead of silently returning the wrong
column.

Only fields Finviz states directly are mapped. Values it does not publish
(absolute debt, cash-flow lines, balance-sheet totals) are left absent rather
than derived from ratios: `src/fundamental/scorer.py` already normalises over the
criteria it can actually measure, so a missing field is handled honestly whereas
a reconstructed one would look like a measurement.
"""

from __future__ import annotations

import re
import threading
import time
from io import StringIO
from typing import Any, Optional

import pandas as pd

from src.data import cache
from src.utils import config
from src.utils.http import get_text
from src.utils.logger import get_logger

log = get_logger(__name__)

QUOTE_URL = "https://finviz.com/quote.ashx?t={ticker}"
SCREENER_URL = "https://finviz.com/screener.ashx"

# Finviz serves 20 screener rows per page and pages by 1-based row offset.
_SCREENER_PAGE_SIZE = 20

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Serialises outbound requests so a 500-ticker sweep cannot burst.
_rate_lock = threading.Lock()
_last_request_at = 0.0

# Finviz label → canonical key in src/fundamental/fetcher.py's payload.
# Only direct statements; ratios that would need reconstruction are omitted.
FIELD_MAP: dict[str, str] = {
    "Price": "price",
    "Market Cap": "market_cap",
    "Shs Outstand": "shares_outstanding",
    "Sales": "revenue",
    "Income": "net_income",
    "EPS (ttm)": "eps",
    "Book/sh": "book_value_per_share",
    "P/E": "pe_ratio",
    "PEG": "peg_ratio",
    "P/S": "ps_ratio",
    "P/B": "pb_ratio",
    "Sales Y/Y TTM": "revenue_growth_yoy",
    "EPS Y/Y TTM": "eps_growth_yoy",
    "EPS next Y": "earnings_growth_rate",
}

# Extra Finviz-native fields kept under their own names — useful for screening
# but not part of the canonical fundamentals contract.
EXTRA_FIELDS: dict[str, str] = {
    "ROA": "roa",
    "ROE": "roe",
    "ROI": "roi",
    "Debt/Eq": "debt_to_equity",
    "LT Debt/Eq": "long_term_debt_to_equity",
    "Current Ratio": "current_ratio",
    "Quick Ratio": "quick_ratio",
    "Gross Margin": "gross_margin_pct",
    "Oper. Margin": "operating_margin_pct",
    "Profit Margin": "profit_margin_pct",
    "Insider Own": "insider_ownership_pct",
    "Inst Own": "institutional_ownership_pct",
    "Short Float": "short_float_pct",
    "Beta": "beta",
    "ATR": "atr",
    "Avg Volume": "avg_volume",
    "Rel Volume": "relative_volume",
    "Volume": "volume",
    "Dividend %": "dividend_yield_pct",
    "Earnings": "earnings_date_raw",
}

_MULTIPLIER = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _parse_number(raw: Any) -> Optional[float]:
    """Parse a Finviz cell: '3013.45B', '12.5%', '1,234', '-', '12.34'.

    Returns None for '-'/blank/unparseable rather than 0.0 — a missing
    fundamental must not read as a measured zero, which would silently score the
    ticker instead of falling back to neutral.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in {"-", "--", "N/A", "NA"}:
        return None
    s = s.replace(",", "")
    percent = s.endswith("%")
    if percent:
        s = s[:-1]
    mult = 1.0
    if s and s[-1].upper() in _MULTIPLIER:
        mult = _MULTIPLIER[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _enabled() -> bool:
    return bool(config.FINVIZ_ENABLED)


def _headers() -> dict[str, str]:
    return {"User-Agent": config.FINVIZ_USER_AGENT, "Accept": "text/html"}


def _throttled_get(url: str) -> Optional[str]:
    """Fetch honouring the minimum inter-request interval. None on any failure."""
    global _last_request_at
    interval = max(0.0, float(config.FINVIZ_MIN_REQUEST_INTERVAL_SEC))
    with _rate_lock:
        wait = interval - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            html = get_text(url, headers=_headers())
        except Exception as exc:
            log.warning(f"Finviz fetch failed ({url}): {exc}")
            return None
        finally:
            _last_request_at = time.monotonic()
    return html


def _tables(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(StringIO(html))
    except Exception as exc:
        log.debug(f"Finviz HTML had no parseable table: {exc}")
        return []


# ── fundamentals ─────────────────────────────────────────────────────────────


def _snapshot_pairs(html: str) -> dict[str, str]:
    """Flatten Finviz's alternating label/value snapshot grid into a mapping.

    The table is located by content — it must contain a label we expect — so a
    restyle yields an empty mapping rather than mis-parsed values.
    """
    known = set(FIELD_MAP) | set(EXTRA_FIELDS)
    for table in _tables(html):
        if table.empty or table.shape[1] < 2:
            continue
        flat = [str(v).strip() for row in table.itertuples(index=False) for v in row]
        labels = {v for v in flat if v in known}
        # Require several known labels so an unrelated table can't match by luck.
        if len(labels) < 5:
            continue
        pairs: dict[str, str] = {}
        for i in range(0, len(flat) - 1, 2):
            label, value = flat[i], flat[i + 1]
            if label in known:
                pairs[label] = value
        if pairs:
            return pairs
    return {}


def fetch_fundamentals(ticker: str, *, use_cache: bool = True) -> Optional[dict]:
    """Fundamentals snapshot for one ticker, or None when unavailable.

    Returns the canonical `src/fundamental/fetcher.py` keys Finviz states
    directly, plus Finviz-native extras under `finviz_*`-free names (see
    EXTRA_FIELDS). Keys Finviz does not publish are simply absent.
    """
    if not _enabled():
        return None
    symbol = str(ticker or "").upper().strip()
    if not _TICKER_RE.match(symbol):
        log.warning(f"Finviz: refusing malformed ticker {ticker!r}")
        return None

    key = f"finviz_fundamentals_{symbol}"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit

    html = _throttled_get(QUOTE_URL.format(ticker=symbol))
    if not html:
        return None
    pairs = _snapshot_pairs(html)
    if not pairs:
        log.warning(f"Finviz: no snapshot table found for {symbol} (layout changed?)")
        return None

    out: dict[str, Any] = {"ticker": symbol, "source": "finviz"}
    for label, value in pairs.items():
        canonical = FIELD_MAP.get(label)
        if canonical:
            out[canonical] = _parse_number(value)
            continue
        extra = EXTRA_FIELDS.get(label)
        if extra == "earnings_date_raw":
            out[extra] = str(value).strip() or None
        elif extra:
            out[extra] = _parse_number(value)

    if use_cache:
        cache.set(key, out, ttl=int(config.FINVIZ_CACHE_TTL_SEC))
    return out


# ── screener ─────────────────────────────────────────────────────────────────


def _tickers_from_screener_table(html: str) -> list[str]:
    """Pull the Ticker column out of a screener results page (by header name)."""
    for table in _tables(html):
        if table.empty:
            continue
        cols = [str(c).strip() for c in table.columns]
        if "Ticker" in cols:
            series = table[cols[cols.index("Ticker")]]
        else:
            # Finviz sometimes renders the header as the first data row.
            first = [str(v).strip() for v in table.iloc[0].tolist()]
            if "Ticker" not in first:
                continue
            series = table.iloc[1:, first.index("Ticker")]
        found = [
            str(v).strip().upper()
            for v in series.tolist()
            if _TICKER_RE.match(str(v).strip().upper())
        ]
        if found:
            return found
    return []


def screen(
    filters: str = "",
    *,
    view: str = "111",
    order: str = "ticker",
    max_results: Optional[int] = None,
    signal: str = "",
) -> list[str]:
    """Run a Finviz screener query and return matching tickers (deduped, ordered).

    ``filters`` is Finviz's own filter string (e.g. ``"cap_midover,fa_pe_u20"``);
    it is passed through rather than wrapped in a DSL, so the full screener
    vocabulary stays available and this module has no filter grammar to drift.
    Returns [] when disabled or on any failure.
    """
    if not _enabled():
        return []

    limit = int(max_results if max_results is not None else config.FINVIZ_SCREENER_MAX_RESULTS)
    limit = max(0, limit)
    if limit == 0:
        return []

    key = f"finviz_screen_{view}_{order}_{signal}_{filters}_{limit}"
    hit = cache.get(key)
    if hit is not None:
        return list(hit)

    out: list[str] = []
    seen: set[str] = set()
    offset = 1
    # Bound the paging independently of the page contents so a layout change
    # (or an endlessly-repeating page) cannot spin forever.
    max_pages = max(1, -(-limit // _SCREENER_PAGE_SIZE))
    for _ in range(max_pages):
        params = [f"v={view}", f"o={order}", f"r={offset}"]
        if filters:
            params.append(f"f={filters}")
        if signal:
            params.append(f"s={signal}")
        html = _throttled_get(f"{SCREENER_URL}?{'&'.join(params)}")
        if not html:
            break
        page = _tickers_from_screener_table(html)
        fresh = [t for t in page if t not in seen]
        if not fresh:
            break  # repeated or empty page — end of results
        for t in fresh:
            seen.add(t)
            out.append(t)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
        offset += _SCREENER_PAGE_SIZE

    if out:
        cache.set(key, out, ttl=int(config.FINVIZ_SCREENER_CACHE_TTL_SEC))
    return out
