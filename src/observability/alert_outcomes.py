"""Alert outcome ledger — does an alert actually precede the move it implies?

The discovery stack fires alerts (movers setups, catalyst wire hits) and until
now nothing ever checked what happened next. Reviewing a morning's alerts meant
hand-comparing the alert list against later quotes — done twice on 2026-08-18,
by hand, and the answer (12 of 25 movers alerts moved in the tagged direction;
median in-direction move −0.1%) was exactly the kind of number this system
should be producing about itself continuously.

This ledger closes that loop. Every fired alert is recorded with its timestamp
and direction; a resolver later reads the 1-minute tape and measures the move
from the alert instant to each configured horizon, **in the direction the alert
implied** (a −5% move after a `short` alert scores +5). The summary the
dashboard shows is then an evidence base that grows with every alert: per
source, per horizon — hit rate, average and median in-direction move, and how
many alerts could not be measured at all.

Honesty rules, in order of importance:

* **The baseline is the first 1m bar close at/after the alert** — not the price
  the discovery list carried (often minutes stale on a fast tape) and not a
  price backfilled later. Same rule for every source, so movers and catalyst
  numbers are comparable.
* **Unmeasurable is a first-class outcome.** An alert near the close whose
  horizon lands after hours, or a halted/illiquid name with no bars, is counted
  as `unresolved` with a reason — never silently dropped, because a scoreboard
  that quietly discards its failures inflates itself.
* **This is telemetry.** Nothing here gates, sizes, or places an order, and
  every write is best-effort — recording an outcome must never break the
  worker loop.

Persistence mirrors ``observability/metrics.py``: JSON file, atomic writes,
interprocess ``flock``, bounded record count.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # non-POSIX — degrade to in-process lock
    fcntl = None  # type: ignore[assignment]

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_WARNED_NO_FLOCK = False

# A baseline bar more than this far after the alert is not "the price when the
# alert fired" — the tape was gapped/halted and the measurement would be a lie.
_BASELINE_SLIP_MIN = 10.0
# A horizon bar this far past its target still honestly represents the horizon
# (one missing bar on a thin name); beyond it the horizon is unresolved.
_HORIZON_SLIP_MIN = 15.0
# Past this, stop trying entirely (session ended, data never coming): the sweep
# marks the horizon unresolved WITHOUT spending a fetch on it.
_EXPIRY_MIN = 360.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso(ts: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _bar_at_or_after(frame, target: datetime, max_slip_min: float):
    """(close, ts) of the first bar at/after ``target`` within the slip, else None.

    Frames come from ``fetch_intraday`` with a naive-UTC index (lookahead.py
    strips tz after converting), so comparison against naive-UTC datetimes is
    the module convention, not an accident.
    """
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        after = frame[frame.index >= target]
        if after.empty:
            return None
        ts = after.index[0].to_pydatetime()
        if (ts - target) > timedelta(minutes=max_slip_min):
            return None
        return float(after["Close"].iloc[0]), ts
    except Exception:
        return None


def _default_fetch(ticker: str):
    from src.data.fetcher import fetch_intraday

    return fetch_intraday(ticker, interval="1m")


class AlertOutcomeLedger:
    """Record fired alerts; resolve their forward moves from the 1m tape."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.ALERT_OUTCOMES_PATH)
        self._records: list[dict] = []
        self._loaded = False
        self._lock = threading.RLock()

    # ── persistence (metrics.py pattern: flock + reload-under-lock + atomic) ─

    def load(self) -> "AlertOutcomeLedger":
        with self._lock:
            self._loaded = True
            self._records = []
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Alert-outcome ledger unreadable ({self._path}): {exc}")
                return self
            if isinstance(raw, list):
                self._records = [r for r in raw if isinstance(r, dict)]
            return self

    @contextmanager
    def _exclusive(self):
        global _WARNED_NO_FLOCK
        with self._lock:
            if fcntl is None:
                if not _WARNED_NO_FLOCK:
                    log.warning("flock unavailable; alert-outcome updates are only "
                                "serialized within this process.")
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
                self.load()
                yield
                self._save_locked()
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    def _save_locked(self) -> None:
        cap = max(1, int(config.ALERT_OUTCOMES_MAX_RECORDS))
        self._records = self._records[-cap:]
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".ao-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._records, f)
            os.replace(tmp, str(p))
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    # ── write path (worker) ──────────────────────────────────────────────────

    def record(self, fired: list[dict], source: str,
               now: Optional[datetime] = None) -> int:
        """Record fired alerts as pending outcomes. Returns how many were added.

        Best-effort and duplicate-tolerant: the same (source, ticker) firing
        again within a minute is one event re-observed, not two alerts.
        """
        if not config.ALERT_OUTCOMES_ENABLED or not fired:
            return 0
        now = now or _utcnow()
        horizons = [int(h) for h in config.ALERT_OUTCOMES_HORIZONS_MIN if int(h) > 0]
        if not horizons:
            return 0
        added = 0
        try:
            with self._exclusive():
                recent_keys = {
                    (r.get("source"), r.get("ticker"))
                    for r in self._records
                    if (ts := _parse_iso(r.get("ts"))) is not None
                    and (now - ts) < timedelta(minutes=1)
                }
                for f in fired:
                    ticker = str(f.get("ticker") or "").upper()
                    if not ticker or (source, ticker) in recent_keys:
                        continue
                    self._records.append({
                        "id": uuid.uuid4().hex[:10],
                        "ts": now.isoformat(),
                        "ticker": ticker,
                        "direction": str(f.get("direction") or "long"),
                        "source": str(source),
                        "score": f.get("score"),
                        "tier": f.get("tier"),
                        "baseline_price": None,
                        "baseline_ts": None,
                        "outcomes": {str(h): None for h in horizons},
                    })
                    recent_keys.add((source, ticker))
                    added += 1
        except Exception as exc:
            log.warning(f"alert-outcomes: record failed ({type(exc).__name__})")
        return added

    # ── resolve path (worker, every cycle) ───────────────────────────────────

    def resolve(self, now: Optional[datetime] = None,
                fetch: Optional[Callable[[str], object]] = None) -> int:
        """Measure due horizons from the 1m tape. Returns horizons settled.

        Two passes. The **sweep** marks anything past the hard expiry as
        unresolved without a fetch, so stale pendings (an alert whose horizon
        landed after the close) cost nothing to retire. The **fetch pass** then
        spends at most ``max_resolve_fetches_per_cycle`` provider calls on the
        oldest due tickers — one fetch resolves that ticker's baseline and every
        due horizon at once. Fail-soft throughout.
        """
        if not config.ALERT_OUTCOMES_ENABLED:
            return 0
        now = now or _utcnow()
        fetch = fetch or _default_fetch
        settled = 0
        try:
            with self._exclusive():
                due_tickers: list[dict] = []
                for rec in self._records:
                    ts = _parse_iso(rec.get("ts"))
                    outcomes = rec.get("outcomes")
                    if ts is None or not isinstance(outcomes, dict):
                        continue
                    pending_due = False
                    for h_key, val in outcomes.items():
                        if val is not None:
                            continue
                        target = ts + timedelta(minutes=int(h_key))
                        if now > target + timedelta(minutes=_EXPIRY_MIN):
                            outcomes[h_key] = {"unresolved": "expired_no_data"}
                            settled += 1
                        elif now >= target:
                            pending_due = True
                    if pending_due and rec not in due_tickers:
                        due_tickers.append(rec)

                cap = max(0, int(config.ALERT_OUTCOMES_MAX_FETCHES_PER_CYCLE))
                for rec in due_tickers[:cap]:
                    settled += self._resolve_record(rec, now, fetch)
        except Exception as exc:
            log.warning(f"alert-outcomes: resolve failed ({type(exc).__name__})")
        return settled

    def _resolve_record(self, rec: dict, now: datetime, fetch) -> int:
        ts = _parse_iso(rec.get("ts"))
        try:
            frame = fetch(rec["ticker"])
        except Exception:
            frame = None
        if frame is None or getattr(frame, "empty", True):
            return 0            # keep pending; the sweep expires it eventually

        settled = 0
        if rec.get("baseline_price") is None:
            base = _bar_at_or_after(frame, ts, _BASELINE_SLIP_MIN)
            if base is None:
                # The tape has bars but none near the alert instant — the alert
                # price is unknowable and so is every horizon measured from it.
                for h_key, val in rec["outcomes"].items():
                    if val is None:
                        rec["outcomes"][h_key] = {"unresolved": "no_baseline_bar"}
                        settled += 1
                return settled
            rec["baseline_price"], base_ts = round(base[0], 4), base[1]
            rec["baseline_ts"] = base_ts.isoformat()

        baseline = float(rec["baseline_price"])
        sign = -1.0 if rec.get("direction") == "short" else 1.0
        for h_key, val in rec["outcomes"].items():
            if val is not None:
                continue
            target = ts + timedelta(minutes=int(h_key))
            if now < target:
                continue
            bar = _bar_at_or_after(frame, target, _HORIZON_SLIP_MIN)
            if bar is None:
                if now > target + timedelta(minutes=_HORIZON_SLIP_MIN):
                    rec["outcomes"][h_key] = {"unresolved": "no_horizon_bar"}
                    settled += 1
                continue
            price, bar_ts = bar
            move = (price - baseline) / baseline * 100.0 if baseline > 0 else 0.0
            rec["outcomes"][h_key] = {
                "price": round(price, 4),
                "ts": bar_ts.isoformat(),
                "move_pct": round(move, 3),
                "in_direction_pct": round(move * sign, 3),
            }
            settled += 1
        return settled

    # ── read path (API/dashboard) ────────────────────────────────────────────

    def summary(self, now: Optional[datetime] = None) -> dict:
        """Per-source, per-horizon evidence: n / hit rate / avg / median.

        ``unresolved`` and ``pending`` ride along at full weight — a hit rate
        whose denominator quietly dropped the unmeasurable alerts would read
        better than the truth.
        """
        with self._lock:
            if not self._loaded:
                self.load()
            records = list(self._records)
        horizons = sorted({h for r in records
                           for h in (r.get("outcomes") or {})}, key=int)
        sources = sorted({r.get("source") for r in records if r.get("source")})
        out: dict = {
            "enabled": bool(config.ALERT_OUTCOMES_ENABLED),
            "total_alerts": len(records),
            "horizons_min": [int(h) for h in horizons],
            "sources": {},
        }
        for source in sources:
            recs = [r for r in records if r.get("source") == source]
            per_h: dict = {}
            for h in horizons:
                vals, pending, unresolved = [], 0, 0
                for r in recs:
                    o = (r.get("outcomes") or {}).get(h)
                    if o is None:
                        pending += 1
                    elif "unresolved" in o:
                        unresolved += 1
                    else:
                        vals.append(float(o["in_direction_pct"]))
                per_h[h] = {
                    "n": len(vals),
                    "pending": pending,
                    "unresolved": unresolved,
                    "hit_rate": (round(sum(1 for v in vals if v > 0) / len(vals), 3)
                                 if vals else None),
                    "avg_in_direction_pct": (round(statistics.fmean(vals), 3)
                                             if vals else None),
                    "median_in_direction_pct": (round(statistics.median(vals), 3)
                                                if vals else None),
                }
            out["sources"][source] = {"alerts": len(recs), "by_horizon": per_h}
        return out

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            if not self._loaded:
                self.load()
            return [dict(r) for r in self._records[-max(0, int(limit)):]][::-1]


_default: Optional[AlertOutcomeLedger] = None
_default_lock = threading.Lock()


def default_ledger() -> AlertOutcomeLedger:
    global _default
    with _default_lock:
        if _default is None:
            _default = AlertOutcomeLedger()
        return _default
