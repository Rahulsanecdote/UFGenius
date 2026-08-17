"""Post-open monitoring + invalidation for discovered movers (Phase 4).

Closes the loop: after a mover is surfaced/alerted, keep watching it and
INVALIDATE the signal when the setup breaks down — momentum turns against it, it
loses VWAP, relative volume fades, or the enriched quality score collapses. This
answers "update or invalidate the signal as market conditions change."

Re-uses Phase 2's intraday enrichment to refresh each watched candidate's live
signals, then applies direction-aware invalidation rules. Discovery/monitoring
only — it never sizes, gates, or places anything. Best-effort throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.scanner.movers import MoverCandidate, _enrich_candidate
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

WATCHING = "watching"
INVALIDATED = "invalidated"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def check_invalidation(
    direction: str,
    rel_volume: float | None,
    momentum_pct: float | None,
    vwap_pct: float | None,
    score: float,
) -> tuple[bool, str]:
    """Direction-aware invalidation check. Returns (invalidated, reason).

    Pure — thresholds come from config; the caller supplies the current signals.
    """
    sign = 1.0 if direction == "long" else -1.0

    # 1. Momentum turned against the setup (long momentum went negative, etc.).
    if momentum_pct is not None:
        aligned_mom = momentum_pct * sign
        if aligned_mom <= float(config.MOVERS_MONITOR_MOMENTUM_FLIP):
            return True, f"momentum turned against the setup ({momentum_pct:+.1f}%)"

    # 2. Lost VWAP — price moved to the wrong side for the setup.
    if config.MOVERS_MONITOR_REQUIRE_VWAP_HOLD and vwap_pct is not None:
        if vwap_pct * sign < 0:
            return True, f"lost VWAP (now {vwap_pct:+.1f}%)"

    # 3. Relative volume faded — the move is losing participation.
    if rel_volume is not None and rel_volume < float(config.MOVERS_MONITOR_RVOL_FLOOR):
        return True, f"relative volume faded to {rel_volume:.1f}x"

    # 4. Quality score collapsed.
    if score < float(config.MOVERS_MONITOR_MIN_SCORE):
        return True, f"quality score fell to {score:.0f}"

    return False, ""


@dataclass
class WatchState:
    """One monitored candidate and its lifecycle."""

    candidate: MoverCandidate
    entry_score: float
    status: str = WATCHING
    reason: str = ""
    updates: int = 0
    first_seen: str = field(default_factory=lambda: _utcnow().isoformat())


class MoversMonitor:
    """Watches candidates and invalidates them as conditions change.

    Reusable across cycles: state lives on the instance so a looped monitor keeps
    tracking the same names. ``evaluate`` re-enriches each still-watching
    candidate and transitions it to ``invalidated`` when a rule trips.
    """

    def __init__(self) -> None:
        self._watch: dict[tuple[str, str], WatchState] = {}

    # ── watch set management ──────────────────────────────────────────────────

    def watch(self, candidates: list[MoverCandidate]) -> int:
        """Add candidates to the watch set (new ones only). Returns how many added."""
        added = 0
        for c in candidates:
            key = (c.ticker, c.direction)
            if key not in self._watch:
                self._watch[key] = WatchState(candidate=c, entry_score=c.score)
                added += 1
        return added

    def active(self) -> list[WatchState]:
        """Still-watching states."""
        return [s for s in self._watch.values() if s.status == WATCHING]

    @staticmethod
    def _halted_symbols() -> dict:
        """Currently-halted symbols, or {} when disabled/unavailable (fail-open).

        One cached lookup per evaluate() covers the whole watch set.
        """
        if not config.MOVERS_HALT_SKIP_INVALIDATION:
            return {}
        try:
            from src.data.halts import active_halts

            return active_halts()
        except Exception as exc:  # never break the monitor loop
            log.debug(f"movers-monitor: halt lookup failed ({type(exc).__name__})")
            return {}

    # ── the monitoring step ───────────────────────────────────────────────────

    def evaluate(self, *, now: datetime | None = None, enrich=None,
                 alert=None) -> list[dict]:
        """Re-evaluate every watched candidate; return the invalidation transitions.

        ``enrich`` (defaults to the Phase-2 intraday enricher) refreshes each
        candidate's live signals in place. ``alert`` (optional) is called with
        the message for each invalidation when
        ``movers.monitor.alert_on_invalidation`` is on.
        """
        now = now or _utcnow()
        enrich = enrich or _enrich_candidate
        transitions: list[dict] = []
        halted = self._halted_symbols()

        for state in list(self._watch.values()):
            if state.status != WATCHING:
                continue
            c = state.candidate
            try:
                enrich(c)   # refresh rel_volume / momentum / vwap / score
            except Exception as exc:  # monitoring must never break the loop
                log.debug(f"movers-monitor: enrich {c.ticker} failed ({type(exc).__name__})")
                continue
            state.updates += 1

            # A halt suppresses the very prints the rules read: no trades means
            # relative volume decays toward zero, which rule 3 would report as
            # "the move is fading". Hold the setup instead of invalidating it on
            # data the market was not allowed to produce.
            record = halted.get(c.ticker)
            if record is not None:
                c.is_halted = True
                c.halt_reason = record.reason
                log.debug(f"movers-monitor: {c.ticker} halted ({record.reason}) — "
                          "holding, invalidation skipped")
                continue
            if c.is_halted:
                c.is_halted = False   # resumed since the last cycle
                c.halt_reason = ""

            invalid, reason = check_invalidation(
                c.direction, c.rel_volume, c.momentum_pct, c.vwap_pct, c.score,
            )
            if invalid:
                state.status = INVALIDATED
                state.reason = reason
                transitions.append({
                    "ticker": c.ticker, "direction": c.direction,
                    "status": INVALIDATED, "reason": reason,
                    "entry_score": state.entry_score, "score": c.score,
                })
                if config.MOVERS_MONITOR_ALERT_ON_INVALIDATION and alert is not None:
                    tag = "🟢 LONG" if c.direction == "long" else "🔴 SHORT"
                    try:
                        alert(
                            f"⚠️ INVALIDATED · {tag} · {c.ticker}\n"
                            f"Reason: {reason}\n"
                            f"Score {state.entry_score:.0f} → {c.score:.0f}. "
                            "Signal no longer valid — stand down.",
                            context=f"mover-invalidated {c.ticker}",
                        )
                    except Exception:  # alerting is best-effort
                        pass

        if transitions:
            log.info(f"movers-monitor: {len(transitions)} invalidated, "
                     f"{len(self.active())} still watching")
        return transitions
