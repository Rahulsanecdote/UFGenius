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

from dataclasses import dataclass, field

from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

_FMP_URL = "https://financialmodelingprep.com/stable/{endpoint}"
# Config source name -> FMP /stable endpoint.
_ENDPOINTS = {
    "gainers": "biggest-gainers",
    "losers": "biggest-losers",
    "most_actives": "most-actives",
}
# Which direction a source implies before we look at the sign of the move.
_SOURCE_DIRECTION = {"gainers": "long", "losers": "short"}


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

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "change_pct": round(self.change_pct, 2),
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


def _fetch_source(source: str) -> list[dict]:
    """Fetch one FMP mover list. Returns [] on no key / any error (never raises)."""
    endpoint = _ENDPOINTS.get(source)
    if endpoint is None:
        log.warning(f"movers: unknown source '{source}' — skipping")
        return []
    key = config.FMP_KEY
    if not key:
        log.debug("movers: FMP_KEY not set — discovery unavailable")
        return []
    try:
        resp = get_retry_session().get(
            _FMP_URL.format(endpoint=endpoint),
            params={"apikey": key},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:  # network / JSON / HTTP — discovery must never break
        log.warning(f"movers: FMP {endpoint} failed ({type(exc).__name__})")
        return []


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
) -> list[MoverCandidate]:
    """Discover and rank today's market movers. Args default to config ``movers:``.

    Returns candidates sorted by discovery score (desc), capped to ``limit``.
    Empty list when discovery is unavailable (no key / provider error).
    """
    sources = sources if sources is not None else config.MOVERS_SOURCES
    min_price = config.MOVERS_MIN_PRICE if min_price is None else min_price
    max_price = config.MOVERS_MAX_PRICE if max_price is None else max_price
    min_change = config.MOVERS_MIN_CHANGE_PCT if min_change_pct is None else min_change_pct
    limit = config.MOVERS_LIMIT if limit is None else limit
    include_short = config.MOVERS_INCLUDE_SHORT if include_short_setups is None else include_short_setups

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
                # Keep the largest-magnitude move and its direction.
                if abs(change) > abs(existing.change_pct):
                    existing.change_pct = change
                    existing.direction = direction

    candidates: list[MoverCandidate] = []
    for c in merged.values():
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
    if config.MOVERS_ENRICH_INTRADAY and candidates:
        cap = max(0, int(config.MOVERS_ENRICH_MAX))
        for c in candidates[:cap]:
            _enrich_candidate(c)
        candidates.sort(key=lambda x: x.score, reverse=True)

    n_enriched = sum(1 for c in candidates if c.enriched)
    log.info(f"movers: {len(candidates)} candidates after filters "
             f"(min_price={min_price}, min_change_pct={min_change}); "
             f"{n_enriched} intraday-enriched")
    return candidates


def get_movers_universe() -> list[str]:
    """Ticker symbols of the discovered movers — the MOVERS universe source."""
    return [c.ticker for c in fetch_market_movers()]
