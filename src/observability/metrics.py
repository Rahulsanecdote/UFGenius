"""
Structured scan metrics (upgrade plan P2.3 — observability stack).

Records one lightweight event per completed universe scan — **latency**, how many
tickers were **scanned**, how many produced a **signal**, the buy-side **label
histogram**, and the **market regime** — and turns the recent history into a
dashboard-ready summary: latency (avg / p95 / last), signal throughput, and a
**data-gap** flag (how long since the last scan, vs a configured ceiling) so an
operator can see the bot has gone quiet.

Deliberately narrow: this is *observability*, not a decision input. Nothing here
places, sizes, or gates an order, and recording is best-effort — a metrics write
must never break a scan. JSON-persisted with atomic writes and bounded, mirroring
the other file-backed ledgers.
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

log = get_logger(__name__)

# Buy-side labels the scanner buckets today (SELL/HOLD are filtered upstream and
# never reach a scan result, so they are not counted here — see the P2.3 note in
# docs/UPGRADE_PLAN.md).
_LABELS = ("STRONG_BUY", "BUY", "WEAK_BUY")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso(ts: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _percentile(sorted_xs: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile of an already-sorted list (empty → None)."""
    if not sorted_xs:
        return None
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    rank = pct / 100.0 * (len(sorted_xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = rank - lo
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * frac


class MetricsLedger:
    """Per-scan metrics ledger (JSON-backed, atomic, bounded)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.METRICS_LEDGER_PATH)
        self._scans: list[dict] = []
        self._loaded = False
        self._lock = threading.RLock()

    def load(self) -> "MetricsLedger":
        with self._lock:
            self._loaded = True
            self._scans = []
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Metrics ledger unreadable ({self._path}): {exc}")
                return self
            if isinstance(raw, list):
                self._scans = [s for s in raw if isinstance(s, dict)]
            return self

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def record_scan(
        self,
        elapsed_sec: float,
        total_scanned: int,
        total_signals: int,
        label_counts: Optional[dict] = None,
        regime: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        """Record one completed scan. Returns the record, or None if uncomputable."""
        try:
            elapsed_f = float(elapsed_sec)
            scanned_i = int(total_scanned)
            signals_i = int(total_signals)
        except (TypeError, ValueError):
            return None
        counts = {lbl: 0 for lbl in _LABELS}
        if isinstance(label_counts, dict):
            for lbl in _LABELS:
                try:
                    counts[lbl] = int(label_counts.get(lbl, 0))
                except (TypeError, ValueError):
                    counts[lbl] = 0
        rec = {
            "ts": (now or _utcnow()).isoformat(),
            "elapsed_sec": round(elapsed_f, 3),
            "total_scanned": scanned_i,
            "total_signals": signals_i,
            "label_counts": counts,
            "regime": str(regime) if regime is not None else None,
        }
        with self._lock:
            self._ensure_loaded()
            self._scans.append(rec)
            cap = max(1, int(getattr(config, "METRICS_MAX_SCANS", 2000)))
            if len(self._scans) > cap:
                self._scans = self._scans[-cap:]
            self._save_locked()
        return rec

    def _save_locked(self) -> None:
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".metrics-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._scans, f, indent=2)
            os.replace(tmp, str(p))
        except Exception as exc:
            log.error(f"Failed to save metrics ledger: {exc}", exc_info=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def scans(self) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            return [dict(s) for s in self._scans]

    def seconds_since_last_scan(self, now: Optional[datetime] = None) -> Optional[float]:
        """Wall-clock seconds since the most recent recorded scan (None if none)."""
        scans = self.scans()
        if not scans:
            return None
        last = _parse_iso(scans[-1].get("ts"))
        if last is None:
            return None
        delta = ((now or _utcnow()) - last).total_seconds()
        return round(max(0.0, delta), 1)

    def summary(self, now: Optional[datetime] = None) -> dict:
        """Aggregate scan metrics for the dashboard / CLI."""
        scans = self.scans()
        n = len(scans)
        if n == 0:
            return {"n_scans": 0, "note": "no scans recorded yet"}

        latencies = sorted(
            float(s.get("elapsed_sec", 0.0)) for s in scans if s.get("elapsed_sec") is not None
        )
        last = scans[-1]
        since = self.seconds_since_last_scan(now)
        gap_threshold = max(0.0, float(getattr(config, "METRICS_DATA_GAP_SECONDS", 3600.0)))
        data_gap = bool(gap_threshold > 0 and since is not None and since > gap_threshold)

        def _avg(xs):
            return round(sum(xs) / len(xs), 2) if xs else None

        return {
            "n_scans": n,
            "last_scan_at": last.get("ts"),
            "seconds_since_last_scan": since,
            "data_gap": data_gap,
            "data_gap_threshold_seconds": gap_threshold,
            "avg_scan_latency_sec": _avg(latencies),
            "p95_scan_latency_sec": (
                round(_percentile(latencies, 95.0), 2) if latencies else None
            ),
            "last_scan_latency_sec": last.get("elapsed_sec"),
            "last_regime": last.get("regime"),
            "last_total_scanned": last.get("total_scanned"),
            "last_total_signals": last.get("total_signals"),
            "last_label_counts": last.get("label_counts"),
            "avg_signals_per_scan": _avg(
                [float(s.get("total_signals", 0)) for s in scans]
            ),
        }


_default: Optional[MetricsLedger] = None
_default_lock = threading.Lock()


def default_ledger() -> MetricsLedger:
    """Process-wide metrics ledger (lazy singleton)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = MetricsLedger().load()
        return _default


def record_scan(*args, **kwargs) -> Optional[dict]:
    """Convenience: record a scan on the default ledger. Best-effort (never raises)."""
    try:
        return default_ledger().record_scan(*args, **kwargs)
    except Exception as exc:  # observability must never break a scan
        log.warning(f"metrics record_scan failed (ignored): {exc}")
        return None


def summary(now: Optional[datetime] = None) -> dict:
    """Scan-metrics summary from the default ledger."""
    return default_ledger().summary(now)
