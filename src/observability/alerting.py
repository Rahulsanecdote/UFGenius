"""
Operational alerting (upgrade plan P2.3 — observability stack).

Fires an operator notification when the bot enters a state that needs attention:

- a **circuit-breaker trip** — the broker-error breaker latching, or the operator
  flipping the global kill switch — so a halt doesn't pass silently, and
- a **data gap** — no scan for longer than the configured ceiling, i.e. the
  scanner has gone dark.

Config-gated and **default off** (`observability.alerts.enabled`): a no-op until
turned on. Every function is best-effort — it returns whether an alert was sent
and never raises, so alerting can never break the path that calls it. It only
observes and notifies; it never gates or places an order.
"""

from __future__ import annotations

from typing import Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _alerts_enabled() -> bool:
    return bool(getattr(config, "OBSERVABILITY_ALERTS_ENABLED", False))


def _send(text: str, context: str) -> bool:
    try:
        from src.alerts.telegram_alert import send_text_alert

        return bool(send_text_alert(text, context=context))
    except Exception as exc:  # transport/import problems must never propagate
        log.warning(f"operational alert failed ({context}): {exc}")
        return False


def alert_breaker_trip(state: dict, kind: str = "breaker") -> bool:
    """Alert that a circuit breaker / kill switch has tripped.

    ``state`` is a ``CircuitBreaker.state()`` snapshot; ``kind`` labels the trip
    (``"manual_halt"`` / ``"broker_breaker"``). Returns True iff an alert was sent.
    """
    if not _alerts_enabled():
        return False
    reason = state.get("manual_halt_reason") if isinstance(state, dict) else None
    count = state.get("broker_error_count") if isinstance(state, dict) else None
    detail = f" reason={reason}" if reason else ""
    if kind == "broker_breaker" and count is not None:
        detail += f" broker_errors={count}"
    msg = f"🛑 UFGenius circuit breaker tripped [{kind}]:{detail or ' new entries blocked'}"
    return _send(msg, context=f"breaker:{kind}")


def alert_data_gap(seconds_since: float, threshold_seconds: float) -> bool:
    """Alert that the scanner has gone quiet longer than the configured ceiling."""
    if not _alerts_enabled():
        return False
    try:
        mins = float(seconds_since) / 60.0
        thr_mins = float(threshold_seconds) / 60.0
    except (TypeError, ValueError):
        return False
    msg = (
        f"⚠️ UFGenius data gap: {mins:.0f} min since last scan "
        f"(threshold {thr_mins:.0f} min) — the scanner may be down."
    )
    return _send(msg, context="data_gap")


def maybe_alert_data_gap(seconds_since: Optional[float]) -> bool:
    """Fire a data-gap alert iff the gap exceeds the configured ceiling.

    A threshold of ``<= 0`` disables gap detection (the default). Numeric
    conversion failures return False, preserving the never-raise contract.
    """
    if not _alerts_enabled() or seconds_since is None:
        return False
    try:
        threshold = max(0.0, float(config.METRICS_DATA_GAP_SECONDS))
        elapsed = float(seconds_since)
    except (TypeError, ValueError):
        return False
    if threshold <= 0 or elapsed <= threshold:
        return False
    return alert_data_gap(elapsed, threshold)
