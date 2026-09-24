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
from dataclasses import dataclass, field
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
# Clock skew between a wire's publisher and us is normally seconds. A timestamp
# further ahead than this is a broken date, and treating it as the freshest
# headline in the set would put it first in every ranking.
_FUTURE_TOLERANCE_HOURS = 0.25


@dataclass
class NewsHeadline:
    title: str
    source: str = ""
    url: str = ""
    published: Optional[datetime] = None
    provider: str = ""  # which fetcher produced it: alpaca | yfinance | newsapi
    # Tickers the wire attached to this story. Populated by the batch/firehose
    # fetch (one story can name several); the per-symbol fetchers leave it empty
    # because the caller already knows the symbol it asked for.
    symbols: list[str] = field(default_factory=list)


# ── classification taxonomy ───────────────────────────────────────────────────
# Order matters: the FIRST tier whose pattern matches any headline wins, and
# dilution outranks everything (an offering headline is the story, whatever
# else the wire says). Patterns are deliberately conservative — a missed
# strong catalyst degrades to a lower tier, which only understates a
# candidate; a false "strong" would overstate one.

# Dilution is the one tier where the conservative direction INVERTS. Missing a
# strong catalyst merely understates a candidate; missing a dilution event
# removes a warning, so these patterns are deliberately broader. They cover the
# instrument regardless of how the press release frames it — AiRWA/OSRH on
# 2026-08-17 announced a "Shareholder Loyalty CVR Program" granting up to five
# additional shares per share (a ~6x share count) and the original patterns read
# it as `none`, i.e. no catalyst and no warning, because none of the words
# "offering", "warrant" or "dilution" appear anywhere in it.
_DILUTION_RE = re.compile(
    r"(\b(offering|registered direct|at-the-market|atm program|dilut\w*|"
    r"warrants?|reverse (stock )?split|shelf registration|"
    r"private placement|equity (line|purchase agreement|distribution agreement)|"
    r"convertible (notes?|debentures?|bonds?|preferred|securit(y|ies))|"
    r"contingent value rights?|share issuance|registration statement|"
    r"prices? .{0,30}(public|direct) offering|"
    r"(issu\w+|grant\w+) .{0,25}(additional|new) shares|"
    r"(additional|new) shares .{0,25}(per|to) (shares?|cvr|holders?|shareholders?)|"
    r"private investment in public equity|"
    # "CVR" needs a qualifier: bare \bCVR\b would flag CVR Energy's earnings
    # as a dilution event. Deliberately NOT matching the bare acronym.
    r"cvr (program|rights?|holders?|record date|tier)|"
    r"pipe (financing|deal|transaction|offering))\b)",
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


# A wire attaches a story to every ticker its BODY names, so a market wrap or a
# movers listicle arrives tagged with a dozen symbols while its headline is
# about one of them at most. Observed 2026-09-24: "Crude Oil Rises Over 4%;
# Darden Earnings Miss Views" was attached to SRZN (the body listed it among the
# day's movers) and classified `moderate` off *Darden's* earnings — a tier SRZN
# never earned, on a day it moved 96% for unrelated reasons.
_CORP_SUFFIX_RE = re.compile(
    r"[\s,\.]+(inc|incorporated|corp|corporation|company|co|ltd|limited|plc|"
    r"llc|l\.?p|holdings?|group|s\.?a|n\.?v|a\.?g|s\.?e|ab|oyj|asa)\b\.?",
    re.IGNORECASE,
)


def _company_core(company_name: str) -> str:
    """The distinctive leading word of a company name, corporate suffix stripped.

    "Surrozen Inc" → "Surrozen"; "Darden Restaurants Inc" → "Darden". Tokens
    under four characters come back empty: a headline is full of ordinary short
    words, and "3M"/"ON" would match constantly.
    """
    name = _CORP_SUFFIX_RE.sub("", str(company_name or "")).strip(" ,.")
    if not name:
        return ""
    first = re.split(r"[\s/&,\-]+", name)[0].strip()
    return first if len(first) >= 4 else ""


def headline_concerns(title: str, symbol: str, company_name: str = "") -> bool:
    """Does this headline actually concern ``symbol``?

    A headline names the security, or it is not about it. Three ways to name it:
    the ticker as a standalone CASE-SENSITIVE token (so "CAT reports" counts and
    "the cat sat" does not), the full company name, or its distinctive leading
    word — the last is what lets "Surrozen Gains Momentum" match the company
    "Surrozen Inc", which a full-name substring test misses because the title
    omits the suffix.

    A heuristic, and deliberately the conservative one for this direction: a
    missed match costs a tier (understating a candidate), while a false match is
    how a different company's earnings become this one's catalyst.
    """
    title = str(title or "")
    if not title:
        return False
    if symbol and re.search(rf"\b{re.escape(symbol)}\b", title):
        return True
    name = str(company_name or "").strip()
    if name and name.lower() in title.lower():
        return True
    core = _company_core(name)
    return bool(core) and re.search(
        rf"\b{re.escape(core)}\b", title, re.IGNORECASE) is not None


def _age_hours(published: Optional[datetime], now: datetime) -> Optional[float]:
    """Hours since publication, or None when there is no usable timestamp."""
    if published is None:
        return None
    try:
        ref = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        return (now - ref).total_seconds() / 3600.0
    except Exception:
        return None


def classify_headlines(
    headlines: list[NewsHeadline],
    *,
    now: Optional[datetime] = None,
    max_age_hours: Optional[float] = None,
    allow_undated: bool = False,
    symbol: str = "",
    company_name: str = "",
    max_story_symbols: Optional[int] = None,
) -> dict:
    """Classify a headline set into a catalyst tier, newest qualifying match wins.

    Returns ``{"tier", "headline", "provider", "published", "age_hours",
    "skipped_stale", "skipped_undated"}``. ``headline`` is the title that
    matched the winning tier (the receipt a human can check) and ``age_hours``
    says how old that receipt is. Tier precedence is fixed:
    dilution > strong > moderate > weak > none.

    Dates are checked HERE, not only at fetch time. This function used to read
    ``h.title`` and nothing else, which left three holes. A headline with no
    timestamp earned full catalyst credit, because every fetcher's cutoff reads
    ``published is not None and published < since`` — so ``None`` sails past a
    window it cannot be measured against. Within a tier the *first* headline in
    list order won rather than the newest, and provider order is not a recency
    guarantee. And the age of the winning headline was never returned, so no
    caller could tell a twenty-minute-old catalyst from a thirty-five-hour-old
    one; the alert formatter printed "just now" for an undated headline, which
    asserts a freshness nobody measured.

    ``max_age_hours=None`` keeps the old behaviour (no age filtering) for
    callers that already filtered upstream. Pass the window and an undated
    headline is skipped rather than credited, since a date-less headline cannot
    satisfy a date window — ``allow_undated=True`` restores the lenient form.
    A timestamp in the future beyond ``_FUTURE_TOLERANCE_HOURS`` is a broken
    date, not a fresh one, and counts as undated.

    SUBJECT. A headline only classifies a security it actually names. Pass
    ``symbol`` (and ``company_name`` when known) and ``headline_concerns`` gates
    every tier — a wire attaches a story to every ticker its BODY mentions, so a
    market wrap arrives tagged with a dozen symbols whose headline concerns one
    of them at most. ``max_story_symbols`` is the blunter gate for callers that
    have no company name to match on (the batch/firehose path carries symbols
    only): a story the wire attached to more tickers than that is a roundup and
    cannot carry a single-name catalyst. Both default off; a caller that passes
    neither behaves as before.

    What this deliberately does NOT solve: a story republished today about an
    old event. On 2026-09-24 a wire piece carrying SRZN's IND submission was
    published at 11:41 ET describing an event from 2026-09-08 that the stock
    had already fallen 2% on. Its publication date was genuinely today — the
    *event* date is in the body text, which this module never fetches. No
    publication-date check can catch that; it needs event-date extraction or
    first-seen tracking across polls.
    """
    now = now or datetime.now(timezone.utc)
    considered: list[tuple[Optional[float], NewsHeadline]] = []
    skipped_stale = skipped_undated = skipped_offtopic = 0
    for h in headlines:
        if max_story_symbols is not None and len(h.symbols) > int(max_story_symbols):
            skipped_offtopic += 1
            continue
        if symbol and not headline_concerns(h.title, symbol, company_name):
            skipped_offtopic += 1
            continue
        age = _age_hours(h.published, now)
        if age is not None and age < -_FUTURE_TOLERANCE_HOURS:
            age = None          # future-dated: the timestamp is wrong, not fresh
        if max_age_hours is not None:
            if age is None:
                if not allow_undated:
                    skipped_undated += 1
                    continue
            elif age > float(max_age_hours):
                skipped_stale += 1
                continue
        considered.append((age, h))

    # Newest first (smaller age = more recent); undated sort last so a dated
    # match is always preferred as the receipt.
    considered.sort(key=lambda p: (p[0] is None, p[0] if p[0] is not None else 0.0))

    base = {"skipped_stale": skipped_stale, "skipped_undated": skipped_undated,
            "skipped_offtopic": skipped_offtopic}
    for tier, pattern in _TIER_PATTERNS:
        for age, h in considered:
            title = (h.title or "").strip()
            if not title or not pattern.search(title):
                continue
            if tier == "strong" and _NEGATION_RE.search(title):
                continue  # adverse phrasing never earns the strong tier
            return {
                "tier": tier,
                "headline": title[:160],
                "provider": h.provider,
                "published": h.published,
                "age_hours": None if age is None else round(age, 2),
                **base,
            }
    return {"tier": "none", "headline": None, "provider": None,
            "published": None, "age_hours": None, **base}


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


def fetch_news_batch(
    symbols: Optional[list[str]] = None,
    *,
    since: Optional[datetime] = None,
    limit: int = 50,
) -> list[NewsHeadline]:
    """Recent stories for MANY symbols in one Alpaca call — or the whole wire.

    The per-symbol fetchers above answer "what is the story on X". This answers
    "what just crossed", which is the question an early alert has to ask: it is
    one request for a whole watchlist, and with ``symbols=None`` it is the
    market-wide firehose, so a name can be surfaced before anyone has listed it
    as a mover.

    Each headline carries the ``symbols`` the wire attached to it, so the caller
    can route a story to a ticker. Alpaca-only (the other providers are
    per-symbol) and fail-soft: no keys or any error yields [].
    """
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        return []
    since = since or (datetime.now(timezone.utc) - timedelta(minutes=15))
    params: dict = {
        "start": since.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "limit": max(1, min(50, int(limit))),
        "sort": "desc",
    }
    if symbols:
        # Bounded: the URL is a GET query, and a runaway watchlist would make it
        # unsendable. The firehose (symbols=None) is the unbounded path.
        params["symbols"] = ",".join(str(s).upper().strip() for s in symbols[:200])
    try:
        resp = get_retry_session().get(
            _ALPACA_NEWS_URL,
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            params=params,
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("news") or []
    except Exception as exc:
        log.debug(f"news batch fetch failed ({type(exc).__name__})")
        return []

    out: list[NewsHeadline] = []
    for it in items:
        title = str(it.get("headline") or "")
        if not title:
            continue
        published = _parse_ts(it.get("created_at"))
        # The server-side `start` is authoritative, but enforce it locally too —
        # the same guarantee the single-symbol paths make.
        if published is not None and published < since:
            continue
        raw_symbols = it.get("symbols")
        out.append(NewsHeadline(
            title=title,
            source=str(it.get("source") or ""),
            url=str(it.get("url") or ""),
            published=published,
            provider="alpaca",
            symbols=[str(s).upper().strip() for s in raw_symbols
                     if str(s).strip()] if isinstance(raw_symbols, list) else [],
        ))
    return out


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
    matches ordinary English (Codex P2). Delegates to the shared subject test,
    which asks the same question the market-wrap gate asks — and which also
    fixes a false negative here: this used to require the FULL company name as a
    substring, so "Surrozen Gains Momentum" failed identity for "Surrozen Inc"
    because the title omits the suffix, and then failed the ticker test too.
    """
    return headline_concerns(title, symbol, company_name)


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
    allow_undated: bool = False,
    require_subject: bool = True,
) -> dict:
    """One-call convenience for the screener: fetch + classify. Never raises.

    ``require_subject`` (default on) means a headline must name this security to
    classify it — the per-symbol path knows both the ticker and the company
    name, so it can make that judgement directly rather than falling back to the
    symbol-count heuristic the batch path needs.

    The window is applied TWICE on purpose: ``fetch_headlines`` uses it to bound
    the request, and it is passed to the classifier as well so the tier is
    decided on headlines that actually fall inside it. The fetch-side cutoff
    cannot do that job alone — it lets an undated headline through, and a cache
    hit replays whatever was stored under the key without re-checking ages.
    """
    try:
        now_utc = now or datetime.now(timezone.utc)
        headlines = fetch_headlines(
            ticker, max_age_hours=max_age_hours, use_cache=use_cache, now=now_utc,
            company_name=company_name,
        )
        return classify_headlines(
            headlines, now=now_utc, max_age_hours=max_age_hours,
            allow_undated=allow_undated,
            symbol=ticker.upper() if require_subject else "",
            company_name=company_name,
        )
    except Exception as exc:
        log.debug(f"{ticker}: catalyst news classification failed ({exc})")
        return {"tier": "none", "headline": None, "provider": None,
                "published": None, "age_hours": None,
                "skipped_stale": 0, "skipped_undated": 0, "skipped_offtopic": 0}
