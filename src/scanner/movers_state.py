"""Shared worker↔web state for the movers pipeline (Phase 7).

The Phase 5 always-on worker runs server-side (a Render Background Worker, a
daemon, or ``bot.py --mode movers-worker``) with no browser attached. Until now
the dashboard had no window into what the worker was actually doing — it ran its
own independent ``/api/movers`` polls and could not show the worker's live
watchlist, the setups it is monitoring, what it just invalidated, or whether it
is even running.

This module is that window: a small JSON snapshot the **worker publishes** after
every cycle and the **dashboard reads** (via ``/api/movers-worker``). It carries
the worker heartbeat, its cycle stats, the live watch set, and bounded rings of
recent alerts and invalidations.

Persistence mirrors the circuit-breaker store (``src/alpaca/circuit_breaker.py``):
JSON on disk, written atomically (tmp file + ``os.replace``) under an
interprocess ``flock`` so a reader never sees a half-written file. It is simpler
than the breaker, though — the worker is the **sole writer** and each publish is
a full-snapshot overwrite, so there is no read-modify-write merge to protect;
readers just load the freshest complete file. Best-effort throughout: a broken or
missing file yields an "unavailable" snapshot and never raises, so a state
problem can neither crash the worker nor take down the dashboard.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
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
    """Naive UTC 'now' — matches the breaker/ledger convention across the app."""
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


def _watch_view(state, prices: Optional[dict] = None) -> dict:
    """Compact, JSON-safe view of one MoversMonitor WatchState.

    When ``prices`` (a PriceStream snapshot: symbol -> {price, age_seconds, ...})
    is supplied and has a fresh tick for this ticker, the live price is attached
    (Phase 8) so the dashboard can show a real-time quote next to the setup.
    """
    c = state.candidate
    view = {
        "ticker": c.ticker,
        "direction": c.direction,
        "entry_score": round(float(state.entry_score), 1),
        "score": round(float(c.score), 1),
        "rel_volume": c.rel_volume,
        "momentum_pct": c.momentum_pct,
        "vwap_pct": c.vwap_pct,
        "is_breakout": bool(c.is_breakout),
        "updates": int(state.updates),
        "first_seen": state.first_seen,
    }
    live = (prices or {}).get(c.ticker)
    if isinstance(live, dict) and live.get("price") is not None:
        view["live_price"] = live.get("price")
        view["live_age_seconds"] = live.get("age_seconds")
        view["live_fresh"] = bool(live.get("fresh"))
    return view


class MoversWorkerState:
    """Persisted snapshot of the always-on movers worker (see module docstring).

    The worker holds one instance and calls :meth:`publish` once per cycle; the
    dashboard constructs a fresh one per request and calls :meth:`load` then
    :meth:`snapshot`. Bounded recent-alert / recent-invalidation rings live on
    the instance so the worker accumulates them across cycles.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.MOVERS_WORKER_STATE_PATH)
        self._lock = threading.RLock()
        self._data: dict = {}
        # Worker-side rings (most-recent-last), bounded on publish.
        self._recent_alerts: list[dict] = []
        self._recent_invalidations: list[dict] = []

    @classmethod
    def load_default(cls) -> "MoversWorkerState":
        """Construct from the configured path and load any current state."""
        return cls().load()

    # ── read path (dashboard) ────────────────────────────────────────────────

    def load(self) -> "MoversWorkerState":
        """Load the snapshot from disk. Missing/malformed → empty (never raises).

        Returns self for one-line ``MoversWorkerState().load()``.
        """
        with self._lock:
            p = Path(self._path)
            if not p.exists():
                self._data = {}
                return self
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Movers worker state unreadable ({self._path}): {exc}")
                self._data = {}
                return self
            self._data = data if isinstance(data, dict) else {}
            # Seed the rings so a worker restart continues the recent history.
            ra = self._data.get("recent_alerts")
            self._recent_alerts = list(ra) if isinstance(ra, list) else []
            ri = self._data.get("recent_invalidations")
            self._recent_invalidations = list(ri) if isinstance(ri, list) else []
            return self

    def snapshot(self, now: Optional[datetime] = None) -> dict:
        """Serializable view for the dashboard / diagnostics.

        Adds a computed ``available`` / ``live`` verdict and ``age_seconds`` so
        the UI can distinguish a running worker from a stopped or stale one
        without re-deriving the staleness rule.
        """
        now = now or _utcnow()
        if not self._data:
            return {
                "available": False,
                "live": False,
                "reason": "No worker state yet — the movers worker is not running "
                          "(start it with `bot.py --mode movers-worker`).",
            }
        updated = _parse_iso(self._data.get("updated_at"))
        age = (now - updated).total_seconds() if updated is not None else None
        stale_after = float(config.MOVERS_WORKER_STATE_STALE_SEC)
        live = age is not None and age <= stale_after
        out = dict(self._data)
        out.update({
            "available": True,
            "live": bool(live),
            "age_seconds": round(age, 1) if age is not None else None,
            "stale_after_seconds": stale_after,
        })
        return out

    # ── write path (worker) ──────────────────────────────────────────────────

    def record_alerts(self, fired: list[dict], now: Optional[datetime] = None) -> None:
        """Append fired alerts to the bounded recent ring (worker-side)."""
        now = now or _utcnow()
        stamp = now.isoformat()
        for f in fired or []:
            self._recent_alerts.append({
                "ticker": f.get("ticker"),
                "direction": f.get("direction"),
                "score": f.get("score"),
                "sent": bool(f.get("sent")),
                "at": stamp,
            })
        self._recent_alerts = self._recent_alerts[-self._max_recent():]

    def record_invalidations(self, transitions: list[dict],
                             now: Optional[datetime] = None) -> None:
        """Append invalidation transitions to the bounded recent ring."""
        now = now or _utcnow()
        stamp = now.isoformat()
        for t in transitions or []:
            row = dict(t)
            row["at"] = stamp
            self._recent_invalidations.append(row)
        self._recent_invalidations = self._recent_invalidations[-self._max_recent():]

    def publish(
        self,
        *,
        cycle: int,
        stats: dict,
        watching: list,
        scan_window_open: bool,
        movers: Optional[list] = None,
        stream_status: Optional[dict] = None,
        stream_prices: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Write a full snapshot of the worker's current state (atomic, flocked).

        ``watching`` is the monitor's active WatchState list; ``movers`` (optional)
        is the last discovered candidate list — both are reduced to compact,
        JSON-safe views here. ``stream_status`` / ``stream_prices`` (Phase 8,
        optional) carry the live-stream status and per-symbol last prices so the
        dashboard can show a real-time tape. Best-effort: any failure is logged,
        never raised.
        """
        now = now or _utcnow()
        try:
            payload = {
                "updated_at": now.isoformat(),
                "cycle": int(cycle),
                "stats": dict(stats or {}),
                "scan_window_open": bool(scan_window_open),
                "watching": [_watch_view(s, stream_prices) for s in (watching or [])],
                "watching_count": len(watching or []),
                "recent_alerts": list(self._recent_alerts),
                "recent_invalidations": list(self._recent_invalidations),
            }
            if stream_status is not None:
                payload["streaming"] = dict(stream_status)
            if movers is not None:
                payload["movers"] = [
                    m.as_dict() if hasattr(m, "as_dict") else m for m in movers
                ][: self._max_recent()]
                payload["movers_count"] = len(movers)
            with self._lock:
                self._data = payload
                self._write(payload)
        except Exception as exc:  # publishing must never break the worker loop
            log.warning(f"movers worker state publish failed ({type(exc).__name__}: {exc})")

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _max_recent() -> int:
        return max(1, int(config.MOVERS_WORKER_STATE_MAX_RECENT))

    def _write(self, payload: dict) -> None:
        """Atomic write (tmp + os.replace) under an interprocess flock."""
        global _WARNED_NO_FLOCK
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        if fcntl is None:
            if not _WARNED_NO_FLOCK:
                log.warning(
                    "flock unavailable on this platform; movers worker state "
                    "writes are only serialized within this process."
                )
                _WARNED_NO_FLOCK = True
            self._atomic_write(p, payload)
            return
        lock_file = open(str(p) + ".lock", "w", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._atomic_write(p, payload)
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    @staticmethod
    def _atomic_write(p: Path, payload: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".mws-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, str(p))
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
