"""Catalyst-triggered alerts — fire on the wire, not on the price.

The movers path is inherently late: a name only reaches it once it has moved
enough to appear on a provider's gainers list, and rediscovery then runs on a
multi-minute cadence. By the time an alert could fire, the move is well under
way (AiRWA/WFF on 2026-08-17 were both found at +100% or more).

This layer watches the **news wire** instead. A strong corporate catalyst —
an FDA approval, an acquisition, a contract award, a guidance raise — is
published at a definite instant, and that instant is *before* the price has
finished reacting. Polling it costs one Alpaca request per cycle for an entire
watchlist, so it can run every worker cycle rather than every fifth.

What this is NOT: prediction. It cannot tell you a stock is about to move. It
tells you a catalyst has just been published, faster than a price-derived
scanner can notice the consequence. Everything downstream is unchanged —
discovery/alerting only, no sizing, no gating, no order ever placed here.

Reuses the existing pieces on purpose: the deterministic tier classifier from
``news_feed`` (so "strong" means exactly what it means to the screener), the
halt map from ``data/halts`` (you cannot act on a halted name), and the
Telegram sender. Opt-in and **default OFF**.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.alerts.telegram_alert import send_text_alert
from src.catalysts.news_feed import (
    NewsHeadline,
    classify_headlines,
    fetch_news_batch,
)
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Tier → how the alert is labelled and which way it leans. Dilution is not a
# catalyst at all but a measured bearish overhang, so it is tagged as such
# rather than being dressed up as an opportunity.
_TIER_TAG = {
    "strong": ("🟢 CATALYST", "long"),
    "moderate": ("🟡 NEWS", "long"),
    "weak": ("⚪ CHATTER", "long"),
    "dilution": ("🔴 DILUTION", "short"),
}

_UNIVERSES = {"watchlist", "all"}

# Config typos are the whole failure mode this module has to defend against:
# every knob is now env-settable from a hosting dashboard, and an unrecognised
# tier or universe simply matches nothing — indistinguishable from "the wire was
# quiet". Warn, once per distinct bad value, so it shows up in the logs without
# a line per poll.
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_catalyst_alert(symbol: str, tier: str, headline: NewsHeadline) -> str:
    """The alert message: tier, ticker, and the headline as a checkable receipt."""
    tag, _ = _TIER_TAG.get(tier, ("📰 NEWS", "long"))
    when = headline.published.astimezone(timezone.utc).strftime("%H:%M UTC") \
        if headline.published else "just now"
    source = f" ({headline.source})" if headline.source else ""
    return (
        f"{tag} · {symbol}\n"
        f"{headline.title}\n"
        f"{when}{source}\n"
        f"Catalyst detected on the wire — NOT a trade instruction, and not a "
        f"prediction that price will follow. Verify before acting."
    )


class CatalystAlerter:
    """Fires deduplicated alerts when a qualifying catalyst hits the wire.

    Reusable across cycles: the dedup lives on the instance, so a looped worker
    will not re-alert the same story. Every failure path is swallowed — a news
    outage must never take the worker down.
    """

    def __init__(self) -> None:
        # (symbol, headline-title) -> epoch seconds of the alert.
        self._recent: dict[tuple[str, str], float] = {}

    def _universe(self) -> Optional[list[str]]:
        """Symbols to filter to, or None for the market-wide firehose."""
        mode = str(config.CATALYST_ALERTS_UNIVERSE or "watchlist").strip().lower()
        if mode not in _UNIVERSES:
            _warn_once(
                f"universe:{mode}",
                f"catalyst-alerts: unknown universe '{mode}' — expected one of "
                f"{sorted(_UNIVERSES)}; falling back to 'watchlist'")
            mode = "watchlist"
        if mode == "all":
            return None
        try:
            from src.data.universe import get_custom_watchlist

            return [s.upper() for s in (get_custom_watchlist() or [])]
        except Exception as exc:
            log.debug(f"catalyst-alerts: watchlist lookup failed ({type(exc).__name__})")
            return []

    @staticmethod
    def _tiers() -> set:
        """Configured tiers, restricted to ones the classifier can actually emit.

        A tier the taxonomy has no name for can never match, so leaving it in
        would just mean "alerts never fire" with nothing in the logs to say why.
        Unknown names are dropped with a warning; if that empties the set the
        caller stops, because alerting on nothing is a misconfiguration, not a
        setting.
        """
        raw = [str(t).strip().lower() for t in (config.CATALYST_ALERTS_TIERS or [])]
        tiers = {t for t in raw if t in _TIER_TAG}
        unknown = sorted({t for t in raw if t and t not in _TIER_TAG})
        if unknown:
            _warn_once(
                f"tiers:{','.join(unknown)}",
                f"catalyst-alerts: unknown tier(s) {unknown} ignored — expected "
                f"any of {sorted(_TIER_TAG)}")
        if not tiers:
            _warn_once(
                "tiers:empty",
                "catalyst-alerts: no valid tiers configured "
                "(catalyst_alerts.tiers / CATALYST_ALERTS_TIERS) — nothing can "
                "alert; set at least one of " + ", ".join(sorted(_TIER_TAG)))
        return tiers

    def _halted(self) -> set:
        if not config.CATALYST_ALERTS_SUPPRESS_HALTED:
            return set()
        try:
            from src.data.halts import active_halts

            return set(active_halts())
        except Exception:
            return set()

    def poll(
        self,
        *,
        now: Optional[datetime] = None,
        send: bool = True,
        fetch: Optional[Callable[..., list[NewsHeadline]]] = None,
    ) -> list[dict]:
        """Check the wire once and alert on qualifying stories.

        Returns the alerts that fired (same dict shape the movers alerter uses,
        so the worker's state ring and stats accept it unchanged). No-op when
        ``catalyst_alerts.enabled`` is off. ``send=False`` formats and records
        without hitting Telegram.
        """
        if not config.CATALYST_ALERTS_ENABLED:
            return []
        now = now or _utcnow()
        fetch = fetch or fetch_news_batch
        tiers = self._tiers()
        if not tiers:
            return []
        cap = max(0, int(config.CATALYST_ALERTS_MAX_PER_RUN))
        ttl = float(config.CATALYST_ALERTS_DEDUP_TTL_SEC)
        lookback = max(30.0, float(config.CATALYST_ALERTS_LOOKBACK_SEC))

        universe = self._universe()
        if universe is not None and not universe:
            # Enabled but watching nothing. This is the default shape of the
            # feature (`universe: watchlist`) on a host where CUSTOM_WATCHLIST
            # was never set, so it deserves a warning, not a debug line — the
            # symptom is otherwise identical to "no news today".
            _warn_once(
                "empty-watchlist",
                "catalyst-alerts: enabled but the watchlist is empty — set "
                "CUSTOM_WATCHLIST, or use universe 'all' "
                "(CATALYST_ALERTS_UNIVERSE=all) for the market-wide wire")
            return []
        try:
            headlines = fetch(universe, since=now - timedelta(seconds=lookback))
        except Exception as exc:  # a news outage must never break the loop
            log.debug(f"catalyst-alerts: fetch failed ({type(exc).__name__})")
            return []

        allowed = set(universe) if universe else None
        halted = self._halted()
        # Drop expired dedup keys before use. The TTL only ever changed the
        # comparison, so entries accumulated for the worker's whole lifetime —
        # unbounded in `universe: all`, where the firehose supplies a fresh
        # (symbol, story) pair for every story on the wire (CodeRabbit).
        now_ts = now.timestamp()
        cutoff = now_ts - max(ttl, 0.0)
        self._recent = {k: seen for k, seen in self._recent.items() if seen >= cutoff}
        fired: list[dict] = []
        for headline in headlines:
            if len(fired) >= cap:
                break
            # Classify this story alone: the tier must describe THIS headline,
            # not the loudest one in the batch.
            tier = classify_headlines([headline]).get("tier", "none")
            if tier not in tiers:
                continue
            for symbol in headline.symbols or []:
                if len(fired) >= cap:
                    break
                if allowed is not None and symbol not in allowed:
                    continue
                if symbol in halted:
                    log.debug(f"catalyst-alerts: {symbol} halted — suppressed")
                    continue
                key = (symbol, headline.title)
                last = self._recent.get(key)
                if last is not None and (now_ts - last) < ttl:
                    continue
                message = format_catalyst_alert(symbol, tier, headline)
                sent = False
                if send:
                    sent = bool(send_text_alert(message, context=f"catalyst {symbol}"))
                self._recent[key] = now_ts
                fired.append({
                    "ticker": symbol,
                    "direction": _TIER_TAG.get(tier, ("", "long"))[1],
                    "score": None,          # news alerts carry no quality score
                    "tier": tier,
                    "headline": headline.title,
                    "message": message,
                    "sent": sent,
                })
        if fired:
            log.info(f"catalyst alerts: {len(fired)} fired "
                     f"({sum(1 for f in fired if f['sent'])} sent)")
        return fired
