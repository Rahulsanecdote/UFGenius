"""Disk-based caching with TTL, size limits, and LRU eviction for market data."""

import hashlib
import pickle
import time
from pathlib import Path
from typing import Any, Optional

_CACHE_DIR = Path(__file__).parent.parent.parent / "data"
_CACHE_DIR.mkdir(exist_ok=True)

DEFAULT_TTL = 86_400        # 24 hours
MAX_CACHE_SIZE_MB = 500     # Evict oldest entries when cache exceeds this


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
        p.unlink(missing_ok=True)
        return None
    return entry["data"]


def set(key: str, data: Any, ttl: int = DEFAULT_TTL) -> None:
    p = _cache_path(key)
    with open(p, "wb") as f:
        pickle.dump({"data": data, "expires": time.time() + ttl}, f)
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
    return sum(p.stat().st_size for p in _CACHE_DIR.glob("*.pkl")) / 1_048_576


def _enforce_size_limit() -> None:
    """If cache exceeds MAX_CACHE_SIZE_MB, evict expired then oldest entries."""
    if _cache_size_mb() <= MAX_CACHE_SIZE_MB:
        return

    # First pass: remove expired
    evict_expired()
    if _cache_size_mb() <= MAX_CACHE_SIZE_MB:
        return

    # Second pass: remove oldest by mtime until under limit
    files = sorted(_CACHE_DIR.glob("*.pkl"), key=lambda p: p.stat().st_mtime)
    for p in files:
        if _cache_size_mb() <= MAX_CACHE_SIZE_MB * 0.8:  # trim to 80% to avoid thrash
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
