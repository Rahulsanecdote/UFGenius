"""
Execution-quality measurement (upgrade plan P2.1).

Records **expected vs realized fill** for every order the executor completes and
turns that into two numbers that matter:

- **Realized slippage** (bps) — how far the fill landed from the price we
  intended (the limit for an entry, the stop/target level for an exit), signed so
  **positive = adverse** (paid more / received less than expected).
- **Implementation shortfall** ($) — that slippage times the shares, i.e. the
  real dollar cost of imperfect execution.

The point (per the plan): *feed the measured slippage back into the backtest cost
model so simulated edge tracks reality* instead of a static guess. A backtest run
with `execution_quality.use_measured_slippage` on reads `measured_slippage_pct()`
once enough real fills exist, falling back to the configured estimate otherwise.

JSON-persisted with atomic writes and bounded, mirroring the other file-backed
ledgers. Pure accounting — it never places or gates an order.
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _adverse_slippage_bps(side: str, expected: float, fill: float) -> Optional[float]:
    """Signed slippage in bps; positive = adverse (worse than expected).

    Buy: adverse when fill > expected. Sell: adverse when fill < expected.
    """
    try:
        expected = float(expected)
        fill = float(fill)
    except (TypeError, ValueError):
        return None
    if expected <= 0:
        return None
    if str(side).lower() == "buy":
        diff = fill - expected
    else:  # sell
        diff = expected - fill
    return diff / expected * 10_000.0


class ExecutionQualityLedger:
    """Per-fill execution-quality ledger (JSON-backed, atomic, bounded)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = str(path or config.EXEC_QUALITY_LEDGER_PATH)
        self._fills: list[dict] = []
        self._loaded = False
        self._lock = threading.RLock()

    def load(self) -> "ExecutionQualityLedger":
        with self._lock:
            self._loaded = True
            self._fills = []
            p = Path(self._path)
            if not p.exists():
                return self
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(f"Execution-quality ledger unreadable ({self._path}): {exc}")
                return self
            if isinstance(raw, list):
                self._fills = [f for f in raw if isinstance(f, dict)]
            return self

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def record_fill(
        self,
        ticker: str,
        side: str,
        kind: str,
        expected_price: float,
        fill_price: float,
        shares: int,
        order_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        """Record one fill's quality. Returns the record, or None if uncomputable."""
        bps = _adverse_slippage_bps(side, expected_price, fill_price)
        if bps is None:
            return None
        try:
            shares_i = int(shares)
            expected_f = float(expected_price)
            fill_f = float(fill_price)
        except (TypeError, ValueError):
            return None
        shortfall = (fill_f - expected_f) * shares_i if str(side).lower() == "buy" \
            else (expected_f - fill_f) * shares_i
        rec = {
            "ts": (now or _utcnow()).isoformat(),
            "ticker": str(ticker).upper(),
            "side": str(side).lower(),
            "kind": str(kind),
            "expected_price": round(expected_f, 4),
            "fill_price": round(fill_f, 4),
            "shares": shares_i,
            "slippage_bps": round(bps, 2),
            "implementation_shortfall": round(shortfall, 2),
            "order_id": str(order_id) if order_id is not None else None,
        }
        with self._lock:
            self._ensure_loaded()
            self._fills.append(rec)
            cap = max(1, int(getattr(config, "EXEC_QUALITY_MAX_FILLS", 5000)))
            if len(self._fills) > cap:
                self._fills = self._fills[-cap:]
            self._save_locked()
        return rec

    def _save_locked(self) -> None:
        p = Path(self._path)
        os.makedirs(p.parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".eq-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._fills, f, indent=2)
            os.replace(tmp, str(p))
        except Exception as exc:
            log.error(f"Failed to save execution-quality ledger: {exc}", exc_info=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def fills(self) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            return [dict(f) for f in self._fills]

    def summary(self) -> dict:
        """Aggregate execution-quality metrics for the dashboard / CLI."""
        fills = self.fills()
        n = len(fills)
        if n == 0:
            return {"n_fills": 0, "note": "no fills recorded yet"}
        bps = [float(f.get("slippage_bps", 0.0)) for f in fills]
        shortfall = [float(f.get("implementation_shortfall", 0.0)) for f in fills]
        entries = [f for f in fills if f.get("kind") == "entry"]
        exits = [f for f in fills if f.get("kind") in ("stop", "target")]

        def _avg(xs):
            return round(sum(xs) / len(xs), 2) if xs else None

        return {
            "n_fills": n,
            "avg_slippage_bps": _avg(bps),
            "avg_entry_slippage_bps": _avg([float(f["slippage_bps"]) for f in entries]),
            "avg_exit_slippage_bps": _avg([float(f["slippage_bps"]) for f in exits]),
            "total_implementation_shortfall": round(sum(shortfall), 2),
            "measured_slippage_pct": self.measured_slippage_pct(),
        }

    def measured_slippage_pct(self) -> Optional[float]:
        """Average ADVERSE slippage as a per-side fraction, for the backtest.

        Returns None until at least ``min_fills_for_measured`` fills exist, so a
        thin sample can't distort the cost model. Negative average slippage
        (execution better than expected) is floored at 0 — the backtest models
        slippage as a cost, never a subsidy.
        """
        fills = self.fills()
        min_fills = int(config.EXEC_QUALITY_MIN_FILLS_FOR_MEASURED)
        if len(fills) < max(1, min_fills):
            return None
        bps = [float(f.get("slippage_bps", 0.0)) for f in fills]
        avg_bps = sum(bps) / len(bps)
        return round(max(0.0, avg_bps) / 10_000.0, 6)


_default: Optional[ExecutionQualityLedger] = None
_default_lock = threading.Lock()


def default_ledger() -> ExecutionQualityLedger:
    """Process-wide execution-quality ledger (lazy singleton)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ExecutionQualityLedger().load()
        return _default


def record_fill(*args, **kwargs) -> Optional[dict]:
    """Convenience: record a fill on the default ledger."""
    return default_ledger().record_fill(*args, **kwargs)


def measured_slippage_pct() -> Optional[float]:
    """Measured adverse slippage fraction from the default ledger (or None)."""
    return default_ledger().measured_slippage_pct()
