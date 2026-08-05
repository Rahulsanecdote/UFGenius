"""In-memory TTL feature cache with bounded size for repeated signal evaluations."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass
class _FeatureEntry:
    value: Any
    expires_at: float
    version: str


class FeatureStore:
    def __init__(self, max_entries: int = 2000):
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, _FeatureEntry] = OrderedDict()
        self._lock = RLock()

    def get(self, key: str, *, version: str) -> Any | None:
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.version != version or now > entry.expires_at:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def set(self, key: str, value: Any, *, ttl_sec: int, version: str) -> None:
        ttl = max(1, int(ttl_sec))
        with self._lock:
            self._entries[key] = _FeatureEntry(
                value=value,
                expires_at=time.time() + ttl,
                version=version,
            )
            self._entries.move_to_end(key)
            self._trim_locked()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self._max_entries,
            }

    def _trim_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
