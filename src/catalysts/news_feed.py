"""News catalyst feed — recent headlines, classified into catalyst tiers.

Fills the pre-market screener's biggest documented gap: catalyst detection was
earnings-calendar-only, so an FDA approval, an M&A bid, or an investor-day
guidance raise read as ``catalyst: unknown``. This module fetches recent
headlines for a ticker (Alpaca News API → yfinance → NewsAPI, first non-empty
wins) and classifies them with a deterministic keyword taxonomy into the tiers
the practitioner canon and the drift/reversion evidence distinguish:

* ``strong``   — hard corporate events with measured post-news drift behind the
                 category: earnings beat / raised guidance, FDA approval or a
                 met endpoint, M&A, a major contract award, a tier-1 upgrade.
* ``moderate`` — real but softer news: earnings mentioned without beat language,
                 coverage initiation, investor day, conference presentation.
* ``weak``     — attention without substance: "why is X soaring" churn pieces,
                 unusual-volume/watchlist listicles — the no-news-pump profile
                 that reverts in the measured record.
* ``dilution`` — offerings / registered directs / warrants / reverse splits.
                 Not a catalyst at all but a measured bearish overhang; callers
                 surface it as a warning flag. Takes precedence over every
                 other tier: a "pricing of offering" headline IS the story.
* ``none``     — nothing usable fetched (which is NOT evidence of no news:
                 every provider here is best-effort and fail-soft).

Classification is keyword-based on purpose: deterministic, offline-testable,
zero model dependencies, and honest about being a heuristic — the tier is a
routing aid for a research screener, not a verified fact. All fetchers go
through ``utils/http``, results are TTL-cached, and every entry point returns
an empty/``none`` result on any failure. Screener-only: nothing here touches
the executor or loosens a filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.data import cache
from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

_ALPACA_NEWS_URL = (
    config.env("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    + "/v1beta1/news"
)

_DEFAULT_MAX_AGE_HOURS = 36.0
_DEFAULT_CACHE_TTL_SEC = 900  # 15 min — headlines move pre-market, but not per-poll
_MAX_HEADLINES = 50


@dataclass
class NewsHeadline:
    title: str
    source: str = ""
    url: str = ""
    published: Optional[datetime] = None
    provider: str = ""  # which fetcher produced it: alpaca | yfinance | newsapi


# ── classification taxonomy ───────────────────────────────────────────────────
# Order matters: the FIRST tier whose pattern matches any headline wins, and
# dilution outranks everything (an offering headline is the story, whatever
# else the wire says). Patterns are deliberately conservative — a missed
# strong catalyst degrades to a lower tier, which only understates a
# candidate; a false "strong" would overstate one.

_DILUTION_RE = re.compile(
    r"\b(offering|registered direct|at-the-market|atm program|dilut\w*|"
    r"warrants?|reverse (stock )?split|shelf registration|prices? .{0,30}"
    r"(public|direct) offering)\b",
    re.IGNORECASE,
)
# Negated/adverse forms: a headline matching this can NEVER classify as
# `strong` — "fails to meet its primary endpoint" contains "meet ... endpoint"
# and would otherwise earn full catalyst credit for an explicitly bad result
# (Codex P1). For a long-continuation screener, adverse events get no credit;
# they fall through to the lower tiers or none.
_NEGATION_RE = re.compile(
    r"\b(fail\w* to|fails?|failed|did not|does not|doesn'?t|will not|won'?t|"
    r"miss(es|ed)?|unable to|falls? short|halt(s|ed)?|terminat\w+|"
    r"discontinu\w+|withdraw\w+|reject\w+|declin\w+ to)\b",
    re.IGNORECASE,
)
_STRONG_RE = re.compile(
    r"\b(beats?( on)? (earnings|estimates|expectations|revenue)|"
    r"(raises?|raised|boosts?|hikes?) .{0,30}(guidance|outlook|forecast)|"
    r"fda (approval|approves|clearance|clears)|"
    r"(meets?|met|achiev\w+) .{0,30}(primary )?endpoint|"
    r"(acquir\w+|merger|buyout|takeover|to acquire|acquisition of)|"
    r"(wins?|awarded|secures?) .{0,30}contract|"
    r"upgrad\w+ (to|by)|price target (raised|boosted|hiked))\b",
    re.IGNORECASE,
)
_MODERATE_RE = re.compile(
    r"\b(earnings|quarterly results|q[1-4] (results|revenue)|"
    r"investor day|analyst day|capital markets day|"
    r"initiat\w+ coverage|coverage initiated|"
    r"partnership|collaboration|"
    r"conference|presents? at|to present)\b",
    re.IGNORECASE,
)
_WEAK_RE = re.compile(
    r"\b(why is .{0,40}(stock )?(soaring|surging|jumping|moving|falling)|"
    r"what'?s going on with|unusual (options|volume)|"
    r"stocks? to watch|watchlist|trending stocks?|meme stock)\b",
    re.IGNORECASE,
)

_TIER_PATTERNS = (
    ("dilution", _DILUTION_RE),
    ("strong", _STRONG_RE),
    ("moderate", _MODERATE_RE),
    ("weak", _WEAK_RE),
)


def classify_headlines(headlines: list[NewsHeadline]) -> dict:
    """Classify a headline set into a catalyst tier.

    Returns ``{"tier", "headline", "provider"}`` where ``headline`` is the
    first title that matched the winning tier (the receipt a human can check).
    Tier precedence is fixed: dilution > strong > moderate > weak > none.
    """
    for tier, pattern in _TIER_PATTERNS:
        for h in headlines:
            title = (h.title or "").strip()
            if not title or not pattern.search(title):
                continue
            if tier == "strong" and _NEGATION_RE.search(title):
                continue  # adverse phrasing never earns the strong tier
            return {"tier": tier, "headline": title[:160], "provider": h.provider}
    return {"tier": "none", "headline": None, "provider": None}


# ── fetchers (each fail-soft: [] on any problem) ─────────────────────────────

def _parse_ts(value) -> Optional[datetime]:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fetch_alpaca(
    symbol: str, since: datetime, company_name: str = ""
) -> list[NewsHeadline]:
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        return []
    try:
        resp = get_retry_session().get(
            _ALPACA_NEWS_URL,
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            params={
                "symbols": symbol,
                "start": since.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "limit": _MAX_HEADLINES,
                "sort": "desc",
            },
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("news") or []
        return [
            NewsHeadline(
                title=str(it.get("headline") or ""),
                source=str(it.get("source") or ""),
                url=str(it.get("url") or ""),
                published=_parse_ts(it.get("created_at")),
                provider="alpaca",
            )
            for it in items
            if it.get("headline")
        ]
    except Exception as exc:
        log.debug(f"{symbol}: Alpaca news fetch failed ({exc})")
        return []


def _fetch_yfinance(
    symbol: str, since: datetime, company_name: str = ""
) -> list[NewsHeadline]:
    try:
        import yfinance as yf

        raw = yf.Ticker(symbol).news or []
        out: list[NewsHeadline] = []
        for it in raw[:_MAX_HEADLINES]:
            # yfinance has shipped two shapes: flat dicts and {"content": {...}}.
            content = it.get("content") if isinstance(it.get("content"), dict) else it
            title = content.get("title") or ""
            if not title:
                continue
            published = _parse_ts(
                content.get("pubDate") or it.get("providerPublishTime")
            )
            if published is not None and published < since:
                continue
            source = ""
            prov = content.get("provider")
            if isinstance(prov, dict):
                source = str(prov.get("displayName") or "")
            elif it.get("publisher"):
                source = str(it["publisher"])
            out.append(NewsHeadline(
                title=str(title), source=source,
                url=str(content.get("canonicalUrl", {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else it.get("link") or ""),
                published=published, provider="yfinance",
            ))
        return out
    except Exception as exc:
        log.debug(f"{symbol}: yfinance news fetch failed ({exc})")
        return []


def _newsapi_identity_ok(title: str, symbol: str, company_name: str) -> bool:
    """Does a keyword-search result actually concern this security?

    NewsAPI is full-text search, not a symbol feed: querying "AI"/"ON"/"CAT"
    matches ordinary English (Codex P2). Accept an article only when the title
    carries the symbol as a standalone CASE-SENSITIVE token, or the company
    name case-insensitively.
    """
    if company_name and company_name.lower() in title.lower():
        return True
    return re.search(rf"\b{re.escape(symbol)}\b", title) is not None


def _fetch_newsapi(
    symbol: str, since: datetime, company_name: str = ""
) -> list[NewsHeadline]:
    if not config.NEWSAPI_KEY:
        return []
    if len(symbol) <= 2 and not company_name:
        # An ultra-short symbol with no company name to validate against is
        # indistinguishable from ordinary English in full-text search — skip
        # rather than mis-attribute (Codex P2).
        return []
    try:
        from newsapi import NewsApiClient

        query = f'"{symbol}"'
        if company_name:
            query += f' OR "{company_name}"'
        client = NewsApiClient(api_key=config.NEWSAPI_KEY)
        response = client.get_everything(
            q=query,
            language="en",
            sort_by="publishedAt",
            from_param=since.strftime("%Y-%m-%dT%H:%M:%S"),
            page_size=_MAX_HEADLINES,
        )
        out: list[NewsHeadline] = []
        for a in response.get("articles") or []:
            title = str(a.get("title") or "")
            if not title or not _newsapi_identity_ok(title, symbol, company_name):
                continue
            published = _parse_ts(a.get("publishedAt"))
            # from_param carries second precision above, but the local cutoff
            # stays as the guarantee — server-side filtering is not trusted
            # to be exact (Codex P2 / CodeRabbit).
            if published is not None and published < since:
                continue
            out.append(NewsHeadline(
                title=title,
                source=str((a.get("source") or {}).get("name") or ""),
                url=str(a.get("url") or ""),
                published=published,
                provider="newsapi",
            ))
        return out
    except Exception as exc:
        log.debug(f"{symbol}: NewsAPI fetch failed ({exc})")
        return []


_FETCHERS = (_fetch_alpaca, _fetch_yfinance, _fetch_newsapi)


def fetch_headlines(
    ticker: str,
    *,
    max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
    use_cache: bool = True,
    cache_ttl_sec: int = _DEFAULT_CACHE_TTL_SEC,
    now: Optional[datetime] = None,
    company_name: str = "",
) -> list[NewsHeadline]:
    """Recent headlines for ``ticker`` — first provider with results wins.

    Provider order: Alpaca News API (real-time, uses the existing keys) →
    yfinance (keyless) → NewsAPI (when configured). Empty list when nothing is
    available — which callers must treat as "no data", never "no news".
    """
    symbol = ticker.upper()
    key = f"news_headlines:{symbol}:{int(max_age_hours)}"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return [NewsHeadline(**h) if isinstance(h, dict) else h for h in hit]

    now_utc = now or datetime.now(timezone.utc)
    since = now_utc - timedelta(hours=max(1.0, float(max_age_hours)))
    for fetcher in _FETCHERS:
        items = fetcher(symbol, since, company_name)
        if items:
            if use_cache:
                cache.set(
                    key,
                    [h.__dict__ for h in items],
                    ttl=max(60, int(cache_ttl_sec)),
                )
            return items
    return []


def catalyst_news_for(
    ticker: str,
    *,
    max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
    use_cache: bool = True,
    now: Optional[datetime] = None,
    company_name: str = "",
) -> dict:
    """One-call convenience for the screener: fetch + classify. Never raises."""
    try:
        headlines = fetch_headlines(
            ticker, max_age_hours=max_age_hours, use_cache=use_cache, now=now,
            company_name=company_name,
        )
        return classify_headlines(headlines)
    except Exception as exc:
        log.debug(f"{ticker}: catalyst news classification failed ({exc})")
        return {"tier": "none", "headline": None, "provider": None}
