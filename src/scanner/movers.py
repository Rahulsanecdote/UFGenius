"""Market-movers discovery — the intraday DISCOVERY source.

Instead of scanning a fixed S&P 500 list, surface the day's actual market-wide
movers (top gainers, losers, and most-actives) from FMP, rank them into a
candidate list with a long/short direction, and hand the tickers to the existing
scan → scoring → RiskGuard pipeline (set ``scan_universe: MOVERS``, or view the
ranked list directly with ``bot.py --mode movers``).

This layer answers "what is moving, and how hard" market-wide. It deliberately
does NOT decide tradeability — the standard disqualification filters and
RiskGuard still run downstream, so most low-quality movers (sub-cap, illiquid,
already-spiked chasers) get filtered out by design. Discovery is broad; the
gates stay strict.

FMP-backed and best-effort: no ``FMP_KEY`` or any request error yields an empty
list and never raises.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from src.scanner.movers_providers import provider_chain
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# The valid discovery source names. Which provider serves each one is the
# chain's business (src/scanner/movers_providers.py).
_ENDPOINTS = frozenset({"gainers", "losers", "most_actives"})
# Which direction a source implies before we look at the sign of the move.
_SOURCE_DIRECTION = {"gainers": "long", "losers": "short"}
_EASTERN = ZoneInfo("America/New_York")

# Per-run source health. THREAD-LOCAL on purpose: on Render the in-process
# worker runs in a daemon thread inside the same gunicorn process that serves
# the dashboard (RUN_WORKER_IN_PROCESS + --threads 4), so two discovery runs
# genuinely overlap — module-global state would let one run clear or append to
# the other's health before the caller read it (CodeRabbit).
_health_state = threading.local()


def _health() -> dict:
    health = getattr(_health_state, "health", None)
    if health is None:
        health = {"attempted": [], "succeeded": [], "failed": [], "served_by": {}}
        _health_state.health = health
    return health


@dataclass
class MoverCandidate:
    """One discovered mover with the metrics behind its rank."""

    ticker: str
    price: float
    change_pct: float
    direction: str            # "long" | "short"
    sources: list[str] = field(default_factory=list)  # lists it appeared in
    name: str = ""
    score: float = 0.0        # 0-100 rank (enriched when intraday data present)
    base_score: float = 0.0   # discovery-only score (before intraday enrichment)

    # Phase 2 — live intraday signals (None until enriched).
    rel_volume: float | None = None    # current bar volume vs recent average
    momentum_pct: float | None = None  # % move over the momentum lookback
    vwap_pct: float | None = None      # % above(+) / below(-) session VWAP
    is_breakout: bool = False
    enriched: bool = False

    # True when the feed's % change was implausible (corporate action) and we
    # replaced it with a split-adjusted recomputation — see _verified_change_pct.
    change_verified: bool = False

    # Trade-halt state (src/data/halts.py). A halted name is untradeable now and
    # its volume signals are suppressed by the halt itself.
    is_halted: bool = False
    halt_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "change_pct": round(self.change_pct, 2),
            "change_verified": self.change_verified,
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "direction": self.direction,
            "sources": list(self.sources),
            "name": self.name,
            "score": round(self.score, 1),
            "base_score": round(self.base_score, 1),
            "rel_volume": self.rel_volume,
            "momentum_pct": self.momentum_pct,
            "vwap_pct": self.vwap_pct,
            "is_breakout": self.is_breakout,
            "enriched": self.enriched,
        }


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def last_source_health() -> dict:
    """Which sources were attempted, succeeded, and failed on this thread's run.

    Without this an upstream failure is indistinguishable from a quiet market:
    every fetcher fails soft to [], so a dead key or an exhausted quota renders
    as "no movers cleared the filters". Callers use it to say which it was —
    and, because successes are tracked too, to tell "everything failed" from
    "one source answered and legitimately had nothing".
    """
    # Copy per type: `served_by` is a mapping, and list()-ing it would silently
    # reduce it to its keys.
    return {key: (dict(value) if isinstance(value, dict) else list(value))
            for key, value in _health().items()}


def last_source_errors() -> list[str]:
    """Just the failures from this thread's most recent discovery run."""
    return list(_health()["failed"])


def _fetch_source(source: str) -> list[dict]:
    """Fetch one mover list, walking the configured provider chain.

    The first provider that actually answers wins. An **empty** answer is a
    real answer — a quiet market — and stops the chain; only a provider that
    *cannot* answer (no key, HTTP error, undocumented payload) falls through.
    That distinction is why the adapters return None-vs-list rather than just a
    list: FMP replies to an exhausted quota with HTTP 200 and a JSON object, so
    "looks successful but isn't" has to be detectable.

    Never raises; records the outcome and the serving provider in the per-run
    health so a soft failure cannot pass for an empty market.
    """
    if source not in _ENDPOINTS:
        log.warning(f"movers: unknown source '{source}' — skipping")
        return []
    health = _health()
    health["attempted"].append(source)

    tried: list[str] = []
    for provider in provider_chain():
        if source not in provider.supports:
            continue          # not a failure — this provider never serves it
        try:
            if not provider.configured():
                continue      # no credentials — skipped, not failed
        except Exception:
            continue
        tried.append(provider.name)
        try:
            rows = provider.fetch(source)
        except Exception as exc:  # an adapter bug must not break discovery
            log.warning(f"movers: provider {provider.name} raised on {source} "
                        f"({type(exc).__name__})")
            rows = None
        if rows is None:
            continue          # could not answer — try the next provider
        health["succeeded"].append(source)
        health["served_by"][source] = provider.name
        if len(tried) > 1:
            log.info(f"movers: {source} served by fallback provider "
                     f"'{provider.name}' after {', '.join(tried[:-1])} could not")
        return rows

    reason = "no_provider_answered" if tried else "no_provider_configured"
    log.warning(f"movers: {source} unavailable — {reason} "
                f"(tried: {', '.join(tried) or 'none'})")
    health["failed"].append(f"{source}: {reason}")
    return []


def _verified_change_pct(ticker: str, price: float) -> float | None:
    """Recompute the move as ``price`` vs OUR SPLIT-ADJUSTED previous close.

    The FMP mover lists report a raw quote change that is NOT adjusted for
    corporate actions, so on the effective date of a reverse split the feed
    reports the mechanical price multiple as if it were a real move: AiRWA
    (YYAI) 1-for-20 on 2026-08-17 surfaced as "+1668%" while the stock was
    actually up ~23%. Our own daily bars come through the provider stack
    split-adjusted, so measuring the feed's quote against our previous close
    puts both operands on one basis — which is how a split-aware quote source
    arrives at +23%.

    Deliberately NOT "the change between the last two closes": that answers a
    different question (the prior completed session) than the field it
    replaces, and pre-market it would overwrite today's move — and the
    direction derived from it — with an unrelated day's (Codex P1).

    Returns None when bars or the reference close are unusable, so the caller
    can fail closed.
    """
    try:
        import pandas as pd

        from src.data.fetcher import fetch_ohlcv

        if price is None or price <= 0:
            return None
        df = fetch_ohlcv(ticker, period="1mo", interval="1d")
        if df is None or df.empty or "Close" not in df:
            return None
        closes = df["Close"].dropna()
        if closes.empty:
            return None
        # The reference must be the PREVIOUS close: once the session has
        # produced its own bar, the last row is today's and measuring against
        # it would report ~0% for every candidate.
        try:
            last_date = pd.Timestamp(closes.index[-1]).date()
            if last_date >= datetime.now(_EASTERN).date():
                closes = closes.iloc[:-1]
        except (TypeError, ValueError):
            pass  # non-datetime index — treat the last row as the prior close
        if closes.empty:
            return None
        prev = float(closes.iloc[-1])
        if prev <= 0:
            return None
        return (price - prev) / prev * 100.0
    except Exception as exc:  # verification is best-effort — never break discovery
        log.debug(f"movers: change verification for {ticker} failed ({type(exc).__name__})")
        return None


def annotate_halts(candidates: list["MoverCandidate"]) -> list["MoverCandidate"]:
    """Flag halted candidates, and drop them when configured to.

    One feed lookup covers the whole list, so this costs a single cached
    request regardless of candidate count. Default is **flag, don't drop**:
    the movers list is discovery, and a halted name is genuinely informative
    (it is usually the day's biggest move) — it just must not be alerted on or
    invalidated. Set ``movers.halts.exclude_from_list`` to remove them instead.
    """
    try:
        from src.data.halts import active_halts

        halted = active_halts()
    except Exception as exc:  # halt lookup must never break discovery
        log.debug(f"movers: halt lookup failed ({type(exc).__name__})")
        return candidates
    if not halted:
        return candidates
    for c in candidates:
        record = halted.get(c.ticker)
        if record is not None:
            c.is_halted = True
            c.halt_reason = record.reason
    n = sum(1 for c in candidates if c.is_halted)
    if n and config.MOVERS_HALT_EXCLUDE_FROM_LIST:
        log.info(f"movers: dropping {n} halted candidate(s) (halts.exclude_from_list)")
        return [c for c in candidates if not c.is_halted]
    if n:
        log.info(f"movers: {n} candidate(s) currently halted — flagged, not alertable")
    return candidates


def _score(change_pct: float, n_sources: int) -> float:
    """Heuristic 0-100 discovery conviction.

    Magnitude of the move dominates; appearing in multiple lists (e.g. a gainer
    that is also a most-active) adds conviction. This is a *discovery* rank — the
    full multi-signal score (rel-volume, momentum, technicals, sentiment) is
    added downstream by the scan pipeline, not here.
    """
    magnitude = min(85.0, abs(change_pct) * 2.5)   # ~34% move saturates the base
    corroboration = min(15.0, 8.0 * (n_sources - 1))
    return round(magnitude + corroboration, 1)


def _enriched_score(direction: str, change_pct: float, rel_volume: float,
                    momentum_pct: float, vwap_pct: float | None, is_breakout: bool) -> float:
    """0-100 rank blending the raw move with LIVE intraday quality signals.

    Deliberately down-weights raw % change (which just says "already up") and
    rewards *early-momentum quality*: unusual relative volume, momentum in the
    setup's direction, and price on the right side of VWAP. So a name up 5% on
    heavy volume above VWAP can outrank one up 40% on no volume below VWAP —
    catching moves nearer their start, not their exhaustion.
    """
    sign = 1.0 if direction == "long" else -1.0
    gap = min(28.0, abs(change_pct) * 0.9)             # magnitude still matters, capped
    rvol = min(30.0, max(0.0, rel_volume) * 7.0)       # rel-vol ~4.3x saturates
    aligned_mom = momentum_pct * sign                  # + when moving the setup's way
    mom = max(-12.0, min(24.0, aligned_mom * 3.0))
    aligned_vwap = (vwap_pct or 0.0) * sign
    vw = max(-8.0, min(12.0, aligned_vwap * 2.0))
    brk = 8.0 if (is_breakout and direction == "long") else 0.0
    return round(max(0.0, min(100.0, gap + rvol + mom + vw + brk)), 1)


def _enrich_candidate(c: "MoverCandidate") -> "MoverCandidate":
    """Attach live intraday signals and recompute the rank. No-op on any failure.

    Reuses the P1.2 intraday scorer and the P1.1 VWAP helper so the metrics match
    the rest of the intraday stack. If intraday data is missing/too thin, the
    candidate keeps its discovery-only base score.
    """
    try:
        from src.data.fetcher import fetch_intraday
        from src.scanner.intraday_scan import score_intraday_frame
        from src.technical.intraday_features import vwap as _vwap

        df = fetch_intraday(c.ticker, interval=config.MOVERS_ENRICH_INTERVAL)
        metrics = score_intraday_frame(df)
        if metrics is None:
            return c  # too few bars — keep base score

        c.rel_volume = metrics.get("rel_volume")
        c.momentum_pct = metrics.get("momentum_pct")
        c.is_breakout = bool(metrics.get("is_breakout"))
        v = _vwap(df)
        last = metrics.get("last_price")
        if v and last:
            c.vwap_pct = round((last - v) / v * 100.0, 2)
        c.enriched = True
        c.score = _enriched_score(
            c.direction, c.change_pct, c.rel_volume or 0.0,
            c.momentum_pct or 0.0, c.vwap_pct, c.is_breakout,
        )
    except Exception as exc:  # enrichment is best-effort — never break discovery
        log.debug(f"movers: intraday enrich for {c.ticker} failed ({type(exc).__name__})")
    return c


def fetch_market_movers(
    *,
    sources: list[str] | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_change_pct: float | None = None,
    limit: int | None = None,
    include_short_setups: bool | None = None,
    enrich: bool | None = None,
) -> list[MoverCandidate]:
    """Discover and rank today's market movers. Args default to config ``movers:``.

    ``enrich`` overrides ``movers.enrich_intraday`` for this call — pass False for
    a fast discovery-only list (a few FMP calls, no per-ticker intraday fetch).
    Returns candidates sorted by score (desc), capped to ``limit``. Empty list
    when discovery is unavailable (no key / provider error).
    """
    sources = sources if sources is not None else config.MOVERS_SOURCES
    min_price = config.MOVERS_MIN_PRICE if min_price is None else min_price
    max_price = config.MOVERS_MAX_PRICE if max_price is None else max_price
    min_change = config.MOVERS_MIN_CHANGE_PCT if min_change_pct is None else min_change_pct
    limit = config.MOVERS_LIMIT if limit is None else limit
    include_short = config.MOVERS_INCLUDE_SHORT if include_short_setups is None else include_short_setups
    enrich = config.MOVERS_ENRICH_INTRADAY if enrich is None else enrich

    # Fresh health for this run, on this thread only.
    _health_state.health = {"attempted": [], "succeeded": [], "failed": [],
                            "served_by": {}}
    merged: dict[str, MoverCandidate] = {}
    for source in sources:
        for row in _fetch_source(source):
            ticker = str(row.get("symbol", "")).upper().strip()
            price = _num(row.get("price"))
            change = _num(row.get("changesPercentage", row.get("changePercentage")))
            if not ticker or price is None or change is None:
                continue

            # Direction: a list's implied side, else the sign of the move
            # (most-actives can move either way).
            direction = _SOURCE_DIRECTION.get(source) or ("short" if change < 0 else "long")

            existing = merged.get(ticker)
            if existing is None:
                merged[ticker] = MoverCandidate(
                    ticker=ticker, price=price, change_pct=change,
                    direction=direction, sources=[source],
                    name=str(row.get("name", "") or ""),
                )
            else:
                if source not in existing.sources:
                    existing.sources.append(source)
                # Keep the largest-magnitude move, with the price and direction
                # from the SAME row: the two endpoints can carry different
                # snapshots, and _verified_change_pct measures the kept change's
                # quote against our previous close — a price from another row
                # would silently make that recomputation wrong (CodeRabbit).
                if abs(change) > abs(existing.change_pct):
                    existing.price = price
                    existing.change_pct = change
                    existing.direction = direction

    candidates: list[MoverCandidate] = []
    suspect = float(config.MOVERS_SUSPECT_CHANGE_PCT)
    for c in merged.values():
        # Corporate-action guard, BEFORE the magnitude/direction filters so the
        # corrected number flows through all of them. A move past `suspect` is
        # far more often a reverse-split artifact than a real session — and an
        # unverifiable extreme claim is dropped rather than published.
        if suspect > 0 and abs(c.change_pct) >= suspect:
            verified = _verified_change_pct(c.ticker, c.price)
            # A recomputation that is ALSO implausible means our own bars have
            # not picked the corporate action up either (provider adjustment
            # lags the effective date) — nothing was verified, so fail closed.
            if verified is None or abs(verified) >= suspect:
                log.warning(
                    f"movers: dropping {c.ticker} — feed change {c.change_pct:+.1f}% "
                    "is implausible for one session and could not be verified "
                    "against split-adjusted bars (corporate action?)"
                )
                continue
            log.warning(
                f"movers: {c.ticker} feed change {c.change_pct:+.1f}% is implausible "
                f"(corporate action?) — using split-adjusted {verified:+.1f}%"
            )
            c.change_pct = verified
            c.change_verified = True
            # The source list's implied direction rested on the bogus number.
            c.direction = "short" if verified < 0 else "long"
        if abs(c.change_pct) < float(min_change):
            continue
        if c.price < float(min_price):
            continue
        if max_price and float(max_price) > 0 and c.price > float(max_price):
            continue
        if c.direction == "short" and not include_short:
            continue
        c.base_score = _score(c.change_pct, len(c.sources))
        c.score = c.base_score
        candidates.append(c)

    # Rank by the discovery score and keep the top `limit` before the (costly)
    # intraday enrichment, so we only fetch bars for names we'd actually return.
    candidates.sort(key=lambda x: x.score, reverse=True)
    if limit and limit > 0:
        candidates = candidates[: int(limit)]

    # Phase 2: enrich the top candidates with live intraday signals and re-rank
    # by early-momentum quality. Bounded by enrich_max; graceful per-candidate.
    if enrich and candidates:
        cap = max(0, int(config.MOVERS_ENRICH_MAX))
        for c in candidates[:cap]:
            _enrich_candidate(c)
        candidates.sort(key=lambda x: x.score, reverse=True)

    # Halt state last: it annotates (and optionally trims) the final list, and
    # costs one cached feed lookup for the whole batch.
    candidates = annotate_halts(candidates)

    n_enriched = sum(1 for c in candidates if c.enriched)
    log.info(f"movers: {len(candidates)} candidates after filters "
             f"(min_price={min_price}, min_change_pct={min_change}); "
             f"{n_enriched} intraday-enriched")
    return candidates


def get_movers_universe() -> list[str]:
    """Ticker symbols of the discovered movers — the MOVERS universe source."""
    return [c.ticker for c in fetch_market_movers()]
