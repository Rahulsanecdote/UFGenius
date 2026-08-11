"""
Circuit breakers for the money path (upgrade plan P0.3).

Three breakers halt **new entries** (never exits — a halt must not strand an
open position without its protective stop):

1. **Global halt switch** — an operator flips `manual_halt` from the dashboard;
   RiskGuard refuses every new entry until it is resumed. This is the emergency
   stop.
2. **Broker-error breaker** — the execution path records each broker failure
   (portfolio read / order submit). When failures within a rolling window reach
   a threshold, new entries are blocked until the failures age out of the
   window (the broker has recovered). Deliberately *not* cleared on a single
   success: a healthy read must not mask a stream of failing order submits.
3. **Data-staleness breaker** — a plan carries `quote_as_of` (the wall-clock
   time its market view was captured). If that is older than the configured
   limit at execution, RiskGuard refuses the entry rather than trade on a stale
   or queued plan. Plans with no timestamp fail **open** (the gate can only fire
   on a known age).

State (the manual-halt flag + the recent broker-error timestamps) is persisted
as JSON. Because the dashboard (gunicorn) and the CLI executor are separate
processes that both mutate this file, every mutation runs as a
reload→modify→save transaction under an **interprocess file lock** (`flock`),
so concurrent writers cannot lose each other's updates — e.g. an executor
recording a broker error can never clobber a halt the dashboard just set. Reads
are fresh (`load_default()`), mirroring how RiskGuard reads the current
portfolio and the realized-P&L ledger on every check. On a platform without
`flock` the transaction degrades to the in-process lock only (logged once).

Thresholds/paths are config-driven (`config.CIRCUIT_*`, `config.yaml`
`circuit_breakers:`), never hardcoded.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.utils import config
from src.utils.logger import get_logger

try:  # POSIX interprocess lock; absent on non-POSIX platforms.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

log = get_logger(__name__)

_WARNED_NO_FLOCK = False


def _utcnow() -> datetime:
    """Naive UTC 'now' — matches the tracker/executor ledger convention.

    The breaker's timestamps are written and read as naive-UTC ISO strings, so
    every datetime compared here must be naive; a tz-aware value would raise
    ``TypeError`` on subtraction and could silently disable a breaker.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso(value) -> Optional[datetime]:
    """Parse an ISO string to a naive-UTC datetime; None on anything unparseable."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


class CircuitBreaker:
    """Persisted trading circuit-breaker state (see module docstring)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.CIRCUIT_STATE_PATH)
        self._lock = threading.RLock()
        self._manual_halt: bool = False
        self._manual_halt_reason: str = ""
        self._manual_halt_at: Optional[str] = None
        self._broker_errors: list[dict] = []  # [{"at": iso, "context": str}]

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load_default(cls) -> "CircuitBreaker":
        """Construct from the configured path and load current state."""
        return cls().load()

    def load(self) -> "CircuitBreaker":
        """Load state from disk. Missing/malformed file → healthy in-memory state.

        Never raises: a broken state file must not take down the money path or
        the dashboard. Returns self for one-line ``CircuitBreaker().load()``.
        """
        with self._lock:
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Circuit-breaker state unreadable ({self._path}): {exc}")
                return self
            if not isinstance(data, dict):
                log.error(
                    f"Circuit-breaker state {self._path} is not a JSON object "
                    f"({type(data).__name__}); ignoring"
                )
                return self
            self._manual_halt = bool(data.get("manual_halt", False))
            self._manual_halt_reason = str(data.get("manual_halt_reason", "") or "")
            at = data.get("manual_halt_at")
            self._manual_halt_at = at if isinstance(at, str) else None
            raw = data.get("broker_errors")
            self._broker_errors = (
                [e for e in raw if isinstance(e, dict) and isinstance(e.get("at"), str)]
                if isinstance(raw, list)
                else []
            )
            return self

    def _save_locked(self) -> None:
        """Atomically write state (tmp file + os.replace). Caller holds the lock."""
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        payload = {
            "manual_halt": self._manual_halt,
            "manual_halt_reason": self._manual_halt_reason,
            "manual_halt_at": self._manual_halt_at,
            "broker_errors": list(self._broker_errors),
        }
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, str(p))
        except Exception as exc:
            log.error(f"Failed to save circuit-breaker state: {exc}", exc_info=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @contextmanager
    def _exclusive(self):
        """Interprocess-serialized reload→mutate→save transaction.

        Holds the in-process lock and an exclusive ``flock`` on a sidecar lock
        file, RELOADS the current on-disk state under the lock, yields for the
        mutation, then atomically saves. Reloading inside the lock is what makes
        each mutation a true read-modify-write: a concurrent process's halt or
        broker error is merged in rather than clobbered. Degrades to the
        in-process lock (with a one-time warning) where ``flock`` is missing.
        """
        global _WARNED_NO_FLOCK
        with self._lock:
            if fcntl is None:
                if not _WARNED_NO_FLOCK:
                    log.warning(
                        "flock unavailable on this platform; circuit-breaker "
                        "updates are only serialized within this process."
                    )
                    _WARNED_NO_FLOCK = True
                self.load()
                yield
                self._save_locked()
                return
            p = Path(self._path)
            os.makedirs(p.parent, exist_ok=True)
            lock_file = open(str(p) + ".lock", "w", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self.load()          # reload freshest on-disk state under lock
                yield
                self._save_locked()
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    # ── manual global halt ───────────────────────────────────────────────────

    def halt(self, reason: str = "") -> None:
        """Engage the global halt switch (blocks all new entries)."""
        with self._exclusive():
            self._manual_halt = True
            self._manual_halt_reason = str(reason or "")
            self._manual_halt_at = _utcnow().isoformat()

    def resume(self) -> None:
        """Release the halt switch and clear the broker-error trail.

        Resuming is a deliberate operator action acknowledging all-clear, so it
        also resets the broker breaker in the same transaction.
        """
        with self._exclusive():
            self._manual_halt = False
            self._manual_halt_reason = ""
            self._manual_halt_at = None
            self._broker_errors = []

    @property
    def manual_halt(self) -> bool:
        return self._manual_halt

    # ── broker-error breaker ─────────────────────────────────────────────────

    def record_broker_error(self, context: str = "", now: Optional[datetime] = None) -> None:
        """Record one broker failure and prune the window.

        Runs under the interprocess transaction so a concurrent halt (or another
        process's broker errors) is preserved, and simultaneous failures don't
        collapse into one.
        """
        with self._exclusive():
            now = now or _utcnow()
            self._broker_errors.append(
                {"at": now.isoformat(), "context": str(context or "")[:200]}
            )
            self._prune_locked(now)

    def _prune_locked(self, now: datetime) -> None:
        window = float(config.CIRCUIT_BROKER_ERROR_WINDOW_SECONDS)
        if window <= 0:
            return
        cutoff = now.timestamp() - window
        self._broker_errors = [
            e for e in self._broker_errors
            if (_parse_iso(e.get("at")) is not None)
            and _parse_iso(e["at"]).timestamp() >= cutoff
        ]

    def broker_error_count(self, now: Optional[datetime] = None) -> int:
        """Number of broker failures within the rolling window."""
        now = now or _utcnow()
        window = float(config.CIRCUIT_BROKER_ERROR_WINDOW_SECONDS)
        if window <= 0:
            return len(self._broker_errors)
        cutoff = now.timestamp() - window
        n = 0
        for e in self._broker_errors:
            ts = _parse_iso(e.get("at"))
            if ts is not None and ts.timestamp() >= cutoff:
                n += 1
        return n

    def broker_breaker_tripped(self, now: Optional[datetime] = None) -> bool:
        threshold = int(config.CIRCUIT_BROKER_ERROR_THRESHOLD)
        if threshold <= 0:
            return False
        return self.broker_error_count(now) >= threshold

    # ── data-staleness breaker ───────────────────────────────────────────────

    @staticmethod
    def _plan_data_age_seconds(plan: dict, now: datetime) -> Optional[float]:
        """Age in seconds of the market data behind a plan, or None if unknown."""
        ts = _parse_iso(plan.get("quote_as_of"))
        if ts is None:
            return None
        return (now - ts).total_seconds()

    def data_stale(self, plan: dict, now: Optional[datetime] = None) -> tuple[bool, Optional[float]]:
        """Return (is_stale, age_seconds). Unknown age fails OPEN (not stale)."""
        max_age = float(config.CIRCUIT_DATA_STALENESS_MAX_SECONDS)
        if max_age <= 0:
            return False, None
        now = now or _utcnow()
        age = self._plan_data_age_seconds(plan, now)
        if age is None:
            return False, None
        return age > max_age, age

    # ── the RiskGuard gate ───────────────────────────────────────────────────

    def blocks_entry(self, plan: dict, now: Optional[datetime] = None) -> tuple[bool, str]:
        """Return (blocked, reason) for a NEW entry. Checked highest-priority-first."""
        now = now or _utcnow()
        if self._manual_halt:
            r = self._manual_halt_reason
            return True, (
                "Trading halted by operator"
                + (f": {r}" if r else "")
                + " (global circuit breaker). Resume from the dashboard."
            )
        if self.broker_breaker_tripped(now):
            n = self.broker_error_count(now)
            window = int(config.CIRCUIT_BROKER_ERROR_WINDOW_SECONDS)
            return True, (
                f"Broker-error circuit breaker tripped ({n} broker failures in "
                f"{window}s); halting new entries until the broker recovers."
            )
        stale, age = self.data_stale(plan, now)
        if stale:
            limit = float(config.CIRCUIT_DATA_STALENESS_MAX_SECONDS)
            return True, (
                f"Market data is stale ({age:.0f}s old > {limit:.0f}s limit); "
                "refusing to enter on stale quotes."
            )
        return False, ""

    # ── dashboard/reporting view ─────────────────────────────────────────────

    def state(self, now: Optional[datetime] = None) -> dict:
        """Serializable snapshot for the dashboard and diagnostics."""
        now = now or _utcnow()
        broker_tripped = self.broker_breaker_tripped(now)
        return {
            "manual_halt": self._manual_halt,
            "manual_halt_reason": self._manual_halt_reason,
            "manual_halt_at": self._manual_halt_at,
            "broker_error_count": self.broker_error_count(now),
            "broker_error_threshold": int(config.CIRCUIT_BROKER_ERROR_THRESHOLD),
            "broker_error_window_seconds": float(config.CIRCUIT_BROKER_ERROR_WINDOW_SECONDS),
            "broker_breaker_tripped": broker_tripped,
            "data_staleness_max_seconds": float(config.CIRCUIT_DATA_STALENESS_MAX_SECONDS),
            # Global (plan-independent) verdict for the panel: staleness is
            # evaluated per-plan at entry time and is not reflected here.
            "entries_blocked": bool(self._manual_halt or broker_tripped),
        }
