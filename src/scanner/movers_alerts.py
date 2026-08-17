"""Screener-style alerts for discovered movers (Phase 3).

Turns ranked ``MoverCandidate``s into human-readable alerts — direction
(long/short), price + move, the supporting intraday metrics, a plain-English
"why", and a confidence label from the rank — and pushes them via the existing
Telegram sender. Opt-in and default OFF; a TTL dedup stops re-alerting the same
ticker on every scan cycle.

Discovery/monitoring layer only: it never sizes, gates, or places anything.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.alerts.telegram_alert import send_text_alert
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _confidence_label(score: float) -> str:
    if score >= 85:
        return "VERY HIGH"
    if score >= 75:
        return "HIGH"
    if score >= 65:
        return "MODERATE"
    return "LOW"


def build_reasons(c) -> list[str]:
    """Plain-English "why this was detected", from the strongest signals."""
    reasons: list[str] = []
    if c.rel_volume is not None and c.rel_volume >= 1.5:
        reasons.append(f"relative volume {c.rel_volume:.1f}x")
    if c.momentum_pct is not None:
        aligned = c.momentum_pct if c.direction == "long" else -c.momentum_pct
        if aligned >= 1.0:
            reasons.append(f"momentum {c.momentum_pct:+.1f}% (with the setup)")
    if c.vwap_pct is not None:
        if c.direction == "long" and c.vwap_pct >= 0:
            reasons.append(f"holding above VWAP (+{c.vwap_pct:.1f}%)")
        elif c.direction == "short" and c.vwap_pct <= 0:
            reasons.append(f"below VWAP ({c.vwap_pct:.1f}%)")
    if c.is_breakout and c.direction == "long":
        reasons.append("intraday breakout")
    reasons.append(f"day move {c.change_pct:+.1f}%")
    if len(c.sources) > 1:
        reasons.append(f"in {len(c.sources)} mover lists ({', '.join(c.sources)})")
    return reasons


def format_alert(c) -> str:
    """The screener alert message for one candidate."""
    tag = "🟢 LONG" if c.direction == "long" else "🔴 SHORT"
    why = " · ".join(build_reasons(c))
    return (
        f"{tag} · {c.ticker}  ${c.price:,.2f}  ({c.change_pct:+.1f}%)\n"
        f"Confidence: {_confidence_label(c.score)} ({c.score:.0f}/100)\n"
        f"Why: {why}\n"
        f"NOT financial advice — screener signal, not a trade instruction."
    )


class MoversAlerter:
    """Fires deduplicated screener alerts for qualifying movers.

    Reusable across scan cycles: the in-memory dedup persists on the instance, so
    a looped scanner won't re-alert the same ticker within ``dedup_ttl_sec``.
    """

    def __init__(self) -> None:
        # (ticker, direction) -> last-alert epoch seconds.
        self._recent: dict[tuple[str, str], float] = {}

    def _eligible(self, c, now: datetime) -> bool:
        # A halted name is untradeable right now, and an alert says "act". Its
        # score is also stale by construction — the halt suppressed the very
        # prints the score was computed from.
        if config.MOVERS_HALT_SUPPRESS_ALERTS and getattr(c, "is_halted", False):
            return False
        if c.score < float(config.MOVERS_ALERTS_MIN_SCORE):
            return False
        if config.MOVERS_ALERTS_REQUIRE_ENRICHED and not c.enriched:
            return False
        key = (c.ticker, c.direction)
        last = self._recent.get(key)
        ttl = float(config.MOVERS_ALERTS_DEDUP_TTL_SEC)
        if last is not None and (now.timestamp() - last) < ttl:
            return False
        return True

    def process(self, candidates: list, *, now: datetime | None = None,
                send: bool = True) -> list[dict]:
        """Alert the qualifying candidates. Returns the alerts that fired.

        No-op (returns []) when ``movers.alerts.enabled`` is off. ``send=False``
        formats + records without hitting Telegram (used by tests / previews).
        """
        if not config.MOVERS_ALERTS_ENABLED:
            return []
        now = now or _utcnow()
        fired: list[dict] = []
        cap = max(0, int(config.MOVERS_ALERTS_MAX_PER_RUN))
        for c in candidates:
            if len(fired) >= cap:
                break
            if not self._eligible(c, now):
                continue
            message = format_alert(c)
            sent = False
            if send:
                # Best-effort — the Telegram sender never raises and no-ops
                # without credentials.
                sent = bool(send_text_alert(message, context=f"mover {c.ticker}"))
            self._recent[(c.ticker, c.direction)] = now.timestamp()
            fired.append({
                "ticker": c.ticker, "direction": c.direction,
                "score": c.score, "message": message, "sent": sent,
            })
        if fired:
            log.info(f"movers alerts: {len(fired)} fired "
                     f"({sum(1 for f in fired if f['sent'])} sent)")
        return fired
