"""Persisted account-equity high-water mark (roadmap Phase 4).

The portfolio risk engine's drawdown guardrail needs a *peak* equity to measure
a peak-to-trough drop, but the broker only reports *current* equity. This tracks
a monotonic high-water mark on disk so the drawdown halt can actually fire.

`observe(equity)` records a fresh reading and returns the running peak; the
engine then computes ``(peak - equity) / peak``. State is a single JSON number
persisted with the same atomic-write + interprocess-`flock` discipline as the
circuit breaker, because the dashboard (advisory endpoint) and the CLI executor
(opt-in gate) both update it from separate processes. Never raises — a broken
store degrades to "no peak known" (drawdown 0), which fails *open* and can only
under-restrict, never falsely halt.

Limitation: the high-water mark starts at the first equity observed after the
store is created, so a drawdown already in progress at first observation is not
seen until equity makes a new high and falls again. This is inherent to a
high-water mark started at deploy and is documented rather than hidden.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
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


class PeakEquityTracker:
    """Monotonic equity high-water mark, persisted and interprocess-safe."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.PORTFOLIO_PEAK_EQUITY_PATH)
        self._lock = threading.RLock()
        self._peak: Optional[float] = None

    @classmethod
    def load_default(cls) -> "PeakEquityTracker":
        return cls().load()

    def load(self) -> "PeakEquityTracker":
        """Load the peak from disk. Missing/malformed → no peak known (None)."""
        with self._lock:
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                peak = float(data.get("peak"))
            except Exception as exc:
                log.error(f"Peak-equity state unreadable ({self._path}): {exc}")
                return self
            self._peak = peak if peak > 0 else None
            return self

    def peak(self) -> Optional[float]:
        return self._peak

    def observe(self, equity: float) -> Optional[float]:
        """Record a fresh equity reading; return the running peak (high-water mark).

        A non-positive/non-finite reading is ignored (returns the stored peak).
        Never raises — on any persistence error the in-memory peak is still
        returned so the caller's drawdown math continues.
        """
        try:
            eq = float(equity)
        except (TypeError, ValueError):
            return self._peak
        if not (math.isfinite(eq) and eq > 0):  # positive and finite (rejects NaN/±inf)
            return self._peak

        try:
            with self._exclusive():
                if self._peak is None or eq > self._peak:
                    self._peak = eq
                    self._save_locked()
            return self._peak
        except Exception as exc:  # persistence must never break the caller
            log.error(f"Peak-equity observe failed: {exc}")
            if self._peak is None or eq > self._peak:
                self._peak = eq
            return self._peak

    # ── persistence ──────────────────────────────────────────────────────────

    def _save_locked(self) -> None:
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".peq-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"peak": self._peak}, f)
            os.replace(tmp, str(p))
        except Exception as exc:
            log.error(f"Failed to save peak-equity state: {exc}", exc_info=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @contextmanager
    def _exclusive(self):
        """Reload→mutate→save under the in-process lock + an exclusive flock.

        Reloading inside the lock makes each update a true read-modify-write, so
        a peak written by the other process is merged (max) rather than clobbered.
        """
        global _WARNED_NO_FLOCK
        with self._lock:
            if fcntl is None:
                if not _WARNED_NO_FLOCK:
                    log.warning(
                        "flock unavailable on this platform; peak-equity updates "
                        "are only serialized within this process."
                    )
                    _WARNED_NO_FLOCK = True
                self.load()
                yield
                return
            p = Path(self._path)
            os.makedirs(p.parent, exist_ok=True)
            lock_path = str(p) + ".lock"
            lock_file = open(lock_path, "w", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self.load()
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
