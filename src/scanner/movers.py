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

import math
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


# Cap on the disclosed withheld list. A run that withholds more names than this
# has a systemic problem the first few entries already show.
_MAX_WITHHELD = 8


def _blank_health() -> dict:
    return {"attempted": [], "succeeded": [], "failed": [], "served_by": {},
            "withheld": []}


def _health() -> dict:
    health = getattr(_health_state, "health", None)
    if health is None:
        health = _blank_health()
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
    # Timestamp (naive UTC) of the LAST CLOSED BAR the enrichment read. Before
    # 09:30 the movers chain serves the PREVIOUS session, and the intraday fetch
    # then returns yesterday's bars — so every enriched metric describes a
    # finished session while the alert presents it as live. `enriched` cannot
    # catch that: there ARE bars, they are just the wrong day. This is what the
    # freshness gate reads. See MoversAlerter._suppression_reason.
    bars_as_of: "datetime | None" = None

    # True when the feed's % change was extreme enough to check and the value
    # shown is our split-adjusted recomputation instead — whether that CORRECTED
    # a corporate-action artifact or CONFIRMED a genuinely huge move. Either way
    # the number came from our own bars, not the feed. See _verified_change_pct.
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
            "bars_as_of": (self.bars_as_of.isoformat()
                           if self.bars_as_of is not None else None),
        }


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # Reject NaN/inf at the merge boundary too, not only in the adapters: this
    # is where candidates are born, and a NaN price defeats every downstream
    # filter (`nan < min` and `nan > max` are both False) so it would reach the
    # ranked list intact. Defence at the layer that actually protects the list.
    if not math.isfinite(out):
        return None
    return out


def last_source_health() -> dict:
    """Which sources were attempted, succeeded, and failed on this thread's run.

    Without this an upstream failure is indistinguishable from a quiet market:
    every fetcher fails soft to [], so a dead key or an exhausted quota renders
    as "no movers cleared the filters". Callers use it to say which it was —
    and, because successes are tracked too, to tell "everything failed" from
    "one source answered and legitimately had nothing".
    """
    # Copy per type: `served_by` is a mapping, and list()-ing it would silently
    # reduce it to its keys. `withheld` holds dicts, which need copying too or
    # a caller's edit reaches back into this thread's state.
    def _copy(value):
        if isinstance(value, dict):
            return dict(value)
        return [dict(v) if isinstance(v, dict) else v for v in value]

    return {key: _copy(value) for key, value in _health().items()}


def last_source_errors() -> list[str]:
    """Just the failures from this thread's most recent discovery run."""
    return list(_health()["failed"])


def last_withheld() -> list[dict]:
    """Movers the corporate-action guard discovered but refused to publish.

    A dropped candidate used to leave no trace, so a name the feed reported at
    +442% was indistinguishable from a name that never appeared — which is how
    CID HoldCo (DAIC) went missing on 2026-08-22 with nothing to point at. The
    guard is still right to withhold an unverifiable number; being unable to
    tell that apart from "never discovered" was not.
    """
    return [dict(entry) for entry in _health()["withheld"]]


def _merge_mode() -> bool:
    """True when every provider is queried and the answers unioned.

    An unknown value is loud rather than silently picking a behaviour, since
    both modes look like "discovery ran" from the outside.
    """
    mode = str(config.MOVERS_PROVIDER_MODE or "merge").strip().lower()
    if mode not in {"merge", "first_wins"}:
        log.warning(f"movers: unknown provider_mode '{mode}' — using 'merge'")
        return True
    return mode == "merge"


def _fetch_source(source: str) -> list[dict]:
    """Fetch one mover list from the configured providers.

    Two modes (``movers.provider_mode``):

    ``merge`` (default) queries EVERY configured provider that serves the
    source and unions the answers. First-wins made the leading provider's
    screener universe the whole candidate pool: a name outside it was invisible
    with nothing recorded anywhere, because it never entered the pipeline at
    all. Observed 2026-09-25 — MSGY ran $2.13 → $5.95 (+202%) and never
    alerted, though replaying its own tape through the live scorer clears the
    alert floor for seven consecutive windows from 10:35 ET (peaking at 100/100
    at 10:45) and the LULD halt that legitimately silences it did not begin
    until ~11:07. Providers disagree about what a "mover" is — universe, float
    and liquidity floors, SIP vs IEX — so the union is the only pool that
    reflects the market rather than one vendor's screener.

    ``first_wins`` is the original chain: the first provider that ANSWERS
    serves the source and the rest are never asked.

    In both modes an **empty** answer is a real answer (a quiet market); only a
    provider that *cannot* answer (no key, HTTP error, undocumented payload)
    is a failure. That distinction is why the adapters return None-vs-list: FMP
    replies to an exhausted quota with HTTP 200 and a JSON object, so "looks
    successful but isn't" has to be detectable.

    Never raises; records the outcome and the serving provider(s) in the per-run
    health so a soft failure cannot pass for an empty market.
    """
    health = _health()
    health["attempted"].append(source)
    if source not in _ENDPOINTS:
        # Record it: returning [] silently would let a typo in movers.sources
        # reach the dashboard as a quiet market rather than a config error
        # (CodeRabbit).
        log.warning(f"movers: unknown source '{source}' — skipping")
        health["failed"].append(f"{source}: unknown_source")
        return []

    merge = _merge_mode()
    tried: list[str] = []
    answered: list[str] = []
    # Held back until we know whether anything answered: a provider failing
    # beside a working one is a PARTIAL outage (the union is missing its
    # names, and the dashboard should read `degraded`), while everything
    # failing is a total one that `no_provider_answered` already states.
    # Recording both would just say it twice.
    partial_failures: list[str] = []
    merged: dict[str, dict] = {}
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
            if merge:
                # Under first-wins this was invisible whenever a later provider
                # answered — the list just quietly lost that vendor's names.
                partial_failures.append(f"{source}: {provider.name}: could_not_answer")
            continue          # could not answer — try the next provider
        answered.append(provider.name)
        if not merge:
            health["succeeded"].append(source)
            health["served_by"][source] = provider.name
            if len(tried) > 1:
                log.info(f"movers: {source} served by fallback provider "
                         f"'{provider.name}' after {', '.join(tried[:-1])} could not")
            return rows
        # Dedupe ACROSS PROVIDERS here, in chain order, so only one row per
        # symbol reaches the cross-source merge in fetch_market_movers. That
        # merge keeps the largest-magnitude change — a rule reasoned about for
        # two ENDPOINTS of one provider, which carry different snapshots of the
        # same feed. Letting rival vendors compete under it would mean the most
        # extreme quote always wins, and since these lists report UNADJUSTED
        # changes, that systematically selects whichever provider is most wrong
        # (the corporate-action guard only re-checks past `suspect_change_pct`,
        # so the whole band below it would silently skew). Chain order is the
        # operator's stated preference and is deterministic; a later provider
        # only ever ADDS symbols the earlier ones did not list.
        for row in rows:
            try:
                symbol = str(row.get("symbol", "")).upper().strip()
            except Exception:
                continue
            if symbol and symbol not in merged:
                merged[symbol] = row

    if merge and answered:
        health["succeeded"].append(source)
        health["served_by"][source] = "+".join(answered)
        health["failed"].extend(partial_failures)
        return list(merged.values())

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


def _record_withheld(c: "MoverCandidate", reason: str, verified: float | None) -> None:
    """Disclose a candidate the corporate-action guard refused to publish."""
    withheld = _health()["withheld"]
    if len(withheld) >= _MAX_WITHHELD:
        return
    withheld.append({
        "ticker": c.ticker,
        "name": c.name,
        "price": round(c.price, 4),
        "reported_change_pct": round(c.change_pct, 2),   # the FEED's claim
        "recomputed_change_pct": (None if verified is None else round(verified, 2)),
        "sources": list(c.sources),
        "reason": reason,      # "unverifiable" | "conflicting"
    })


def _changes_agree(feed_pct: float, verified_pct: float, tol: float) -> bool:
    """Do the feed's change and our recomputation describe the SAME move?

    Compared *relatively*, against the larger of the two: at the magnitudes this
    guard sees (a 300%+ claim) a fixed number of percentage points would mean
    nothing. The two operands measure the same quote and differ only in their
    previous-close basis, so on an ordinary session they land within rounding of
    each other, while a reverse split separates them by the split ratio — YYAI's
    1-for-20 gave +1668% against +23%, a relative gap of 0.99. Opposite signs
    can never agree; that falls out of the arithmetic without its own branch.

    ``tol`` is a fraction: 0 demands an exact match, 1.0 accepts any two values
    that merely share a sign.
    """
    if not (math.isfinite(feed_pct) and math.isfinite(verified_pct)):
        return False
    scale = max(abs(feed_pct), abs(verified_pct))
    if scale <= 0:
        return True                       # both flat — nothing to disagree about
    return abs(feed_pct - verified_pct) / scale <= tol


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


def _last_bar_time(df) -> "datetime | None":
    """Timestamp of the frame's last bar, naive UTC, or None if unreadable.

    ``fetch_intraday`` hands back a naive-UTC index (``lookahead.py`` converts
    then strips the tz), which is the convention the freshness check assumes.
    """
    try:
        ts = df.index[-1]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
    except Exception:
        return None


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
        c.bars_as_of = _last_bar_time(df)
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
    _health_state.health = _blank_health()
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
    agree_tol = float(config.MOVERS_SUSPECT_AGREEMENT_PCT) / 100.0
    for c in merged.values():
        # Corporate-action guard, BEFORE the magnitude/direction filters so the
        # corrected number flows through all of them. A move past `suspect` is
        # more often a reverse-split artifact than a real session — but not
        # always, so the claim is CHECKED against our own split-adjusted bars
        # rather than assumed false. Three outcomes, and only one keeps the
        # feed's magnitude.
        if suspect > 0 and abs(c.change_pct) >= suspect:
            verified = _verified_change_pct(c.ticker, c.price)
            if verified is None:
                # Nothing to check against — fail closed rather than publish an
                # extreme claim on the feed's word alone. Recorded, not silent:
                # this is the branch DAIC hit, and an unexplained absence is
                # indistinguishable from never having been discovered.
                log.warning(
                    f"movers: withholding {c.ticker} — feed change {c.change_pct:+.1f}% "
                    "is implausible for one session and could not be checked "
                    "against split-adjusted bars (corporate action?)"
                )
                _record_withheld(c, "unverifiable", None)
                continue
            if _changes_agree(c.change_pct, verified, agree_tol):
                # Our own split-adjusted bars independently reproduce the move,
                # which is corroboration, not grounds for rejection: a reverse
                # split would have pushed the two apart by the split ratio.
                # Rejecting agreement is what hid CID HoldCo (DAIC) on
                # 2026-08-22 — a genuine $0.43 → $2.31 on 6M shares.
                log.info(
                    f"movers: {c.ticker} feed change {c.change_pct:+.1f}% is extreme but "
                    f"confirmed by split-adjusted bars ({verified:+.1f}%) — keeping"
                )
            elif abs(verified) >= suspect:
                # They disagree AND the recomputation is itself implausible: our
                # bars have not picked the corporate action up either (provider
                # adjustment lags the effective date), so neither number is
                # trustworthy and nothing was verified.
                log.warning(
                    f"movers: withholding {c.ticker} — feed change {c.change_pct:+.1f}% and "
                    f"split-adjusted {verified:+.1f}% disagree, and both are implausible "
                    "for one session (corporate action?)"
                )
                _record_withheld(c, "conflicting", verified)
                continue
            else:
                log.warning(
                    f"movers: {c.ticker} feed change {c.change_pct:+.1f}% is implausible "
                    f"(corporate action?) — using split-adjusted {verified:+.1f}%"
                )
            # Both surviving branches publish the recomputation: it is the value
            # on OUR basis, which is the basis every downstream consumer uses.
            c.change_pct = verified
            c.change_verified = True
            # A corrected move can flip sign, and the source list's implied
            # direction rested on the number we just replaced.
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
