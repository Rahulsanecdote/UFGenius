"""Disk-based caching with TTL, size limits, and LRU eviction for market data."""

import hashlib
import pickle
import threading
import time
from pathlib import Path
from typing import Any, Optional

_CACHE_DIR = Path(__file__).parent.parent.parent / "data"
_CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_TTL = 86_400        # 24 hours
MAX_CACHE_SIZE_MB = 500     # Evict oldest entries when cache exceeds this

# The size sweep runs after every set() and the scan pool calls set() from
# 8 workers at once (audit M9): serialize eviction so concurrent sweeps don't
# fight over the same files.
_EVICTION_LOCK = threading.Lock()


def _cache_path(key: str) -> Path:
    h = hashlib.md5(key.encode()).hexdigest()
    return _CACHE_DIR / f"{h}.pkl"


def get(key: str) -> Optional[Any]:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            entry = pickle.load(f)
    except Exception:
        p.unlink(missing_ok=True)
        return None
    if time.time() > entry["expires"]:
        # Fresh miss, but DO NOT delete: the stale-fallback path
        # (get_stale / get_metadata) relies on expired entries surviving.
        # Cleanup happens via evict_expired() and the size-limit sweep.
        return None
    return entry["data"]


def get_stale(key: str) -> Optional[Any]:
    """Return cached data ignoring expiry (for degraded/offline fallback).

    Returns None only when the entry is absent or unreadable. Unlike get(),
    this never deletes and never checks TTL — it is the last-resort source
    when a live provider fetch fails.
    """
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            entry = pickle.load(f)
    except Exception:
        return None
    return entry.get("data")


def get_metadata(key: str, allow_expired: bool = True) -> Optional[dict]:
    """Return freshness metadata for a cache entry, or None if absent.

    Keys: ``age_sec`` (seconds since the entry was written), ``expires``
    (epoch), ``is_expired`` (bool). When ``allow_expired`` is False, an expired
    entry is reported as absent (returns None).
    """
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            entry = pickle.load(f)
    except Exception:
        return None
    now = time.time()
    expires = entry.get("expires", 0)
    is_expired = now > expires
    if is_expired and not allow_expired:
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None  # evicted between load and stat — report absent
    return {
        "age_sec": max(0.0, now - mtime),
        "expires": expires,
        "is_expired": is_expired,
    }


def set(key: str, data: Any, ttl: int = DEFAULT_TTL) -> None:
    p = _cache_path(key)
    # Atomic write: serialize to a temp file in the cache dir, then os.replace.
    # Prevents a concurrent reader from loading a half-written pickle and
    # unlinking a file another worker is mid-write.
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(_CACHE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump({"data": data, "expires": time.time() + ttl}, f)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _enforce_size_limit()


def evict_expired() -> int:
    """Remove all expired cache entries. Returns count removed."""
    removed = 0
    now = time.time()
    for p in _CACHE_DIR.glob("*.pkl"):
        try:
            with open(p, "rb") as f:
                entry = pickle.load(f)
            if now > entry["expires"]:
                p.unlink(missing_ok=True)
                removed += 1
        except Exception:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def _cache_size_mb() -> float:
    # A file can vanish between glob() and stat() when another worker evicts
    # it (audit M9) — skip it instead of blowing up the set() that swept.
    total = 0
    for p in _CACHE_DIR.glob("*.pkl"):
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total / 1_048_576


def _mtime_or_zero(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0  # vanished mid-sweep — sorts first, unlink is a no-op


def _enforce_size_limit() -> None:
    """If cache exceeds MAX_CACHE_SIZE_MB, evict expired then oldest entries."""
    if _cache_size_mb() <= MAX_CACHE_SIZE_MB:
        return

    with _EVICTION_LOCK:
        # Re-check under the lock — a concurrent sweep may have already trimmed.
        if _cache_size_mb() <= MAX_CACHE_SIZE_MB:
            return

        # First pass: remove expired
        evict_expired()
        if _cache_size_mb() <= MAX_CACHE_SIZE_MB:
            return

        # Second pass: remove oldest by mtime until under the trim target
        # (a fraction of the limit, so we don't re-sweep on every write).
        from src.utils import config

        ratio = min(max(float(config.CACHE_EVICTION_TARGET_RATIO), 0.1), 1.0)
        files = sorted(_CACHE_DIR.glob("*.pkl"), key=_mtime_or_zero)
        for p in files:
            if _cache_size_mb() <= MAX_CACHE_SIZE_MB * ratio:
                break
            p.unlink(missing_ok=True)


def clear_all() -> None:
    for p in _CACHE_DIR.glob("*.pkl"):
        p.unlink(missing_ok=True)


def stats() -> dict:
    """Return cache statistics."""
    files = list(_CACHE_DIR.glob("*.pkl"))
    now = time.time()
    expired = 0
    for p in files:
        try:
            with open(p, "rb") as f:
                entry = pickle.load(f)
            if now > entry["expires"]:
                expired += 1
        except Exception:
            expired += 1
    return {
        "total_entries": len(files),
        "expired_entries": expired,
        "size_mb": round(_cache_size_mb(), 2),
        "max_size_mb": MAX_CACHE_SIZE_MB,
    }
