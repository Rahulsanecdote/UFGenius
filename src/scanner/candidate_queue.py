"""
Candidate queue for the continuous intraday scanner (upgrade plan P1.2).

The continuous scan loop (producer) discovers tradable candidates every cycle;
the entry logic (P1.3, consumer) drains them. This is the thread-safe, bounded,
**deduplicated** buffer between them:

- **Deduplicated** — the same ticker+kind found on cycle after cycle is not
  re-emitted within ``dedup_ttl_sec``; otherwise a persistently-unusual name
  would flood the queue and the consumer.
- **Bounded** — capped at ``maxlen``; the oldest candidate is dropped when full,
  so a stalled consumer can never grow memory without limit.
- **Thread-safe** — the producer loop and the consumer run on different threads.

No I/O and no wall-clock dependence beyond an injectable ``now`` clock, so it is
deterministic to test.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Candidate:
    """One scan hit awaiting entry evaluation."""

    ticker: str
    kind: str            # "gap" | "volume" | "momentum" | "breakout"
    metric: float        # the headline number that fired (e.g. rel-volume, % move)
    detected_at: str     # naive-UTC ISO timestamp
    payload: dict = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        """Dedup identity: one live candidate per (ticker, kind)."""
        return (self.ticker.upper(), self.kind)

    def to_dict(self) -> dict:
        return asdict(self)


class CandidateQueue:
    """Thread-safe, bounded, de-duplicating queue of scan candidates."""

    def __init__(
        self,
        maxlen: int = 500,
        dedup_ttl_sec: float = 300.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._items: deque[Candidate] = deque(maxlen=max(1, int(maxlen)))
        self._dedup_ttl = max(0.0, float(dedup_ttl_sec))
        self._last_seen: dict[tuple[str, str], datetime] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def push(self, candidate: Candidate, now: Optional[datetime] = None) -> bool:
        """Add a candidate unless an identical (ticker, kind) is still fresh.

        Returns True if enqueued, False if suppressed as a duplicate within the
        dedup window.
        """
        now = now or self._clock()
        key = candidate.key()
        with self._lock:
            last = self._last_seen.get(key)
            if last is not None and self._dedup_ttl > 0:
                if (now - last).total_seconds() < self._dedup_ttl:
                    return False
            self._last_seen[key] = now
            self._items.append(candidate)
            self._prune_seen_locked(now)
            return True

    def _prune_seen_locked(self, now: datetime) -> None:
        """Drop dedup entries older than the TTL so the map stays bounded."""
        if self._dedup_ttl <= 0:
            return
        stale = [
            k for k, ts in self._last_seen.items()
            if (now - ts).total_seconds() >= self._dedup_ttl
        ]
        for k in stale:
            self._last_seen.pop(k, None)

    def drain(self) -> list[Candidate]:
        """Atomically remove and return all queued candidates (FIFO order)."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            return out

    def snapshot(self) -> list[Candidate]:
        """Return the current candidates without removing them."""
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
