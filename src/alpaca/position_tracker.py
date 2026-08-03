"""
Live position state machine, persisted as JSON.

Tracks the full lifecycle of each open position:
  pending_fill → (entry order fills) → active → (all exits) → closed

The JSON file at LIVE_POSITION_STORE_PATH survives bot restarts so state
is not lost between scheduled scans or crashes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Default path relative to project root; overridden by config constant.
_DEFAULT_STORE_PATH = str(
    Path(__file__).parent.parent.parent / "data" / "live_positions.json"
)


@dataclass
class LivePosition:
    """Full lifecycle state for one live position."""

    ticker: str
    entry_order_id: str

    # Prices & sizing
    entry_price: float          # Planned entry (limit price from trade plan)
    fill_price: Optional[float] # Actual fill price (None until filled)
    shares_initial: int         # Original full position size
    shares_open: int            # Remaining open shares (decreases as targets hit)
    risk_dollars: float         # (entry − stop) × shares_initial

    # Stop-loss
    stop_price: float
    stop_order_id: Optional[str]

    # T1 target (30% of position)
    t1_price: float
    t1_shares: int
    t1_order_id: Optional[str]
    t1_hit: bool

    # T2 target (40% of position)
    t2_price: float
    t2_shares: int
    t2_order_id: Optional[str]
    t2_hit: bool

    # T3 target (remaining ~30%)
    t3_price: float
    t3_shares: int
    t3_order_id: Optional[str]
    t3_hit: bool

    opened_at: str          # ISO-8601 UTC timestamp
    status: str             # "pending_fill" | "active" | "closed"
    trades_today_date: str  # YYYY-MM-DD (for daily trade count)


def _allocate_exit_tranches(shares: int) -> tuple[int, int, int]:
    """
    Allocate shares across three exit tranches: T1≈30%, T2≈40%, T3=remainder.

    Each tranche is clamped to the shares still available, so the three tranches
    ALWAYS sum to exactly `shares` and never over-allocate. For a 1-share
    position this yields (1, 0, 0) — the old code returned (1, 1, 0), which sums
    to 2 and caused the monitor to submit sell orders for more shares than were
    held (overselling / accidental short).

    Args:
        shares: Total shares in the position (>= 0).

    Returns:
        (t1_shares, t2_shares, t3_shares) — always sums to `shares`.
    """
    if shares <= 0:
        return (0, 0, 0)
    t1 = min(shares, max(1, int(round(shares * 0.30))))
    t2 = min(shares - t1, max(0, int(round(shares * 0.40))))
    t3 = shares - t1 - t2
    return t1, t2, t3


class PositionTracker:
    """JSON-backed live position state machine."""

    def __init__(self, store_path: Optional[str] = None) -> None:
        self._path = store_path or getattr(
            config, "LIVE_POSITION_STORE_PATH", _DEFAULT_STORE_PATH
        )
        self._positions: dict[str, LivePosition] = {}
        # Per-day count of ENTRY events, keyed by YYYY-MM-DD. Counted separately
        # from `_positions` because the position map is keyed by ticker: a
        # same-day close-then-re-enter of one ticker is two entries but one
        # record, so counting records would let it bypass max_trades_per_day.
        self._daily_entries: dict[str, int] = {}
        # Reentrant: mutators call save() while holding the lock, and the monitor
        # thread mutates the same instance as the main thread (audit: tracker
        # thread-safety). RLock lets a thread re-acquire without deadlocking.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Load positions from JSON.  Missing file → empty tracker (not an error).

        Each record is parsed independently so a single malformed/legacy entry
        skips only itself instead of discarding every tracked position (which
        would then be overwritten on the next save, silently abandoning live
        broker positions and their stops). Stale closed records from previous
        days are pruned to bound file growth, while today's closed records are
        kept so ``trades_today`` stays accurate.
        """
        with self._lock:
            path = Path(self._path)
            if not path.exists():
                log.debug(f"Position store not found at {self._path} — starting fresh")
                return
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.error(
                    f"Failed to read position store ({self._path}): {exc}", exc_info=True
                )
                self._positions = {}
                return

            # A valid-JSON-but-non-object store (e.g. a list) must fail safe, not
            # raise on .items() outside the read handler and abort startup.
            if not isinstance(data, dict):
                log.error(
                    f"Position store {self._path} is not a JSON object "
                    f"({type(data).__name__}); starting empty"
                )
                self._positions = {}
                return

            # v2 wraps positions + daily entry counts; legacy is a flat
            # ticker->record map. Detect v2 by the "positions" object.
            if isinstance(data.get("positions"), dict):
                raw_positions = data["positions"]
                raw_daily = data.get("daily_entries")
                self._daily_entries = dict(raw_daily) if isinstance(raw_daily, dict) else {}
            else:
                raw_positions = data
                self._daily_entries = {}

            positions: dict[str, LivePosition] = {}
            skipped = 0
            for ticker, entry in raw_positions.items():
                try:
                    positions[ticker] = LivePosition(**entry)
                except Exception as exc:
                    skipped += 1
                    log.error(f"Skipping unreadable position record {ticker!r}: {exc}")
            self._positions = positions
            if skipped:
                log.warning(f"Position store: skipped {skipped} unreadable record(s)")
            self._prune_stale_closed()

            # Backfill today's entry count from records (covers legacy stores and
            # keeps the counter >= records opened today), and drop other days so
            # the counter map stays bounded.
            today = date.today().isoformat()
            record_today = sum(
                1 for p in self._positions.values() if p.trades_today_date == today
            )
            count_today = max(int(self._daily_entries.get(today, 0)), record_today)
            self._daily_entries = {today: count_today} if count_today else {}

            log.info(f"Loaded {len(self._positions)} position(s) from {self._path}")

    def _prune_stale_closed(self) -> None:
        """Drop closed positions not opened today (bounds store growth)."""
        today = date.today().isoformat()
        stale = [
            t
            for t, p in self._positions.items()
            if p.status == "closed" and p.trades_today_date != today
        ]
        for t in stale:
            self._positions.pop(t, None)

    def save(self) -> None:
        """Atomically write all positions to JSON (write-tmp then rename)."""
        with self._lock:
            path = Path(self._path)
            os.makedirs(path.parent, exist_ok=True)
            tmp = str(path) + ".tmp"
            try:
                payload = {
                    "positions": {
                        ticker: dataclasses.asdict(pos)
                        for ticker, pos in self._positions.items()
                    },
                    "daily_entries": dict(self._daily_entries),
                }
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp, str(path))
            except Exception as exc:
                log.error(f"Failed to save position store: {exc}", exc_info=True)
                if os.path.exists(tmp):
                    os.remove(tmp)

    # ------------------------------------------------------------------ #
    # Mutations                                                            #
    # ------------------------------------------------------------------ #

    def add_position(self, plan: dict, entry_order_id: str) -> LivePosition:
        """
        Create a new LivePosition from a trade plan and persist it.

        Args:
            plan:            Trade plan dict from generate_trade_plan().
            entry_order_id:  Alpaca order ID of the submitted entry order.

        Returns:
            The newly created LivePosition.

        Raises:
            ValueError: If a position for this ticker is already being tracked.
        """
        ticker = plan["ticker"]
        with self._lock:
            if ticker in self._positions and self._positions[ticker].status != "closed":
                raise ValueError(f"Position already tracked for {ticker}")

        position_info = plan.get("position", {})
        entry_price = float(plan["entry"]["price"])
        shares_initial = int(position_info.get("shares", 1))
        risk_dollars = float(position_info.get("risk_dollars", 0.0))
        stop_price = float(plan["stop_loss"]["price"])
        targets = plan.get("targets", {})

        t1_shares, t2_shares, t3_shares = _allocate_exit_tranches(shares_initial)

        pos = LivePosition(
            ticker=ticker,
            entry_order_id=entry_order_id,
            entry_price=entry_price,
            fill_price=None,
            shares_initial=shares_initial,
            shares_open=shares_initial,
            risk_dollars=risk_dollars,
            stop_price=stop_price,
            stop_order_id=None,
            t1_price=float(targets.get("T1", {}).get("price", entry_price)),
            t1_shares=t1_shares,
            t1_order_id=None,
            t1_hit=False,
            t2_price=float(targets.get("T2", {}).get("price", entry_price)),
            t2_shares=t2_shares,
            t2_order_id=None,
            t2_hit=False,
            t3_price=float(targets.get("T3", {}).get("price", entry_price)),
            t3_shares=t3_shares,
            t3_order_id=None,
            t3_hit=False,
            opened_at=datetime.utcnow().isoformat(),
            status="pending_fill",
            trades_today_date=date.today().isoformat(),
        )
        with self._lock:
            self._positions[ticker] = pos
            # Count this as a distinct entry EVENT (survives a same-day re-entry
            # that replaces a closed record for the same ticker).
            today = date.today().isoformat()
            self._daily_entries[today] = self._daily_entries.get(today, 0) + 1
            self.save()
        log.info(
            f"Position tracked: {ticker} | {shares_initial} shares"
            f" | entry={entry_price:.2f} stop={stop_price:.2f}"
            f" | T1={pos.t1_price:.2f}({t1_shares}sh)"
            f" T2={pos.t2_price:.2f}({t2_shares}sh)"
            f" T3={pos.t3_price:.2f}({t3_shares}sh)"
        )
        return pos

    def mark_entry_filled(
        self, ticker: str, fill_price: float, shares: int
    ) -> None:
        """Record actual fill price/qty and transition status to 'active'."""
        with self._lock:
            pos = self._require(ticker)
            pos.fill_price = fill_price
            pos.shares_initial = shares
            pos.shares_open = shares
            # Recompute tranche sizes against the actual fill qty
            pos.t1_shares, pos.t2_shares, pos.t3_shares = _allocate_exit_tranches(shares)
            pos.status = "active"
            self.save()
        log.info(f"{ticker}: entry filled @ ${fill_price:.2f} x{shares}")

    def mark_stop_placed(self, ticker: str, order_id: str) -> None:
        """Record the Alpaca order ID of the stop-loss order."""
        with self._lock:
            pos = self._require(ticker)
            pos.stop_order_id = order_id
            self.save()

    def mark_target_placed(self, ticker: str, level: Literal["t1", "t2", "t3"], order_id: str) -> None:
        """
        Record the order ID for a target limit sell order.

        Args:
            ticker: Ticker symbol.
            level:  One of "t1", "t2", "t3".
            order_id: Alpaca order ID.
        """
        with self._lock:
            pos = self._require(ticker)
            setattr(pos, f"{level}_order_id", order_id)
            self.save()

    def mark_target_hit(self, ticker: str, level: Literal["t1", "t2", "t3"]) -> None:
        """
        Record that a target exit was filled.

        Reduces shares_open by the tranche size and sets the hit flag.

        Args:
            ticker: Ticker symbol.
            level:  One of "t1", "t2", "t3".
        """
        with self._lock:
            pos = self._require(ticker)
            sold = int(getattr(pos, f"{level}_shares"))
            pos.shares_open = max(0, pos.shares_open - sold)
            setattr(pos, f"{level}_hit", True)
            remaining = pos.shares_open
            self.save()
        log.info(
            f"{ticker}: {level.upper()} hit — {sold} shares sold,"
            f" {remaining} remaining"
        )

    def mark_closed(self, ticker: str, reason: str) -> None:
        """Mark position as fully closed."""
        with self._lock:
            pos = self._require(ticker)
            pos.shares_open = 0
            pos.status = "closed"
            self.save()
        log.info(f"{ticker}: position closed (reason={reason})")

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def get(self, ticker: str) -> Optional[LivePosition]:
        """Return position or None if not tracked."""
        with self._lock:
            return self._positions.get(ticker)

    def get_open(self) -> dict[str, LivePosition]:
        """Return all positions with status != 'closed'."""
        with self._lock:
            return {t: p for t, p in self._positions.items() if p.status != "closed"}

    def has_open(self, ticker: str) -> bool:
        """True if an OPEN (pending_fill/active) position exists for `ticker`.

        Closed records are ignored so a ticker can be re-entered after its prior
        position has fully exited (the duplicate guard must not block forever).
        """
        with self._lock:
            pos = self._positions.get(ticker)
            return pos is not None and pos.status != "closed"

    def trades_today(self) -> int:
        """Count ENTRY events made today (not records), so same-day re-entries
        of a closed ticker still count toward max_trades_per_day."""
        today = date.today().isoformat()
        with self._lock:
            return int(self._daily_entries.get(today, 0))

    def remove(self, ticker: str) -> None:
        """Permanently remove a position from the tracker."""
        with self._lock:
            self._positions.pop(ticker, None)
            self.save()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _require(self, ticker: str) -> LivePosition:
        pos = self._positions.get(ticker)
        if pos is None:
            raise KeyError(f"No tracked position for {ticker}")
        return pos
