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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

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


class PositionTracker:
    """JSON-backed live position state machine."""

    def __init__(self, store_path: Optional[str] = None) -> None:
        self._path = store_path or getattr(
            config, "LIVE_POSITION_STORE_PATH", _DEFAULT_STORE_PATH
        )
        self._positions: dict[str, LivePosition] = {}

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def load(self) -> None:
        """Load positions from JSON.  Missing file → empty tracker (not an error)."""
        path = Path(self._path)
        if not path.exists():
            log.debug(f"Position store not found at {self._path} — starting fresh")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._positions = {
                ticker: LivePosition(**entry) for ticker, entry in data.items()
            }
            log.info(
                f"Loaded {len(self._positions)} position(s) from {self._path}"
            )
        except Exception as exc:
            log.error(
                f"Failed to load position store ({self._path}): {exc}", exc_info=True
            )
            self._positions = {}

    def save(self) -> None:
        """Atomically write all positions to JSON (write-tmp then rename)."""
        path = Path(self._path)
        os.makedirs(path.parent, exist_ok=True)
        tmp = str(path) + ".tmp"
        try:
            payload = {
                ticker: dataclasses.asdict(pos)
                for ticker, pos in self._positions.items()
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
        if ticker in self._positions:
            raise ValueError(f"Position already tracked for {ticker}")

        position_info = plan.get("position", {})
        entry_price = float(plan["entry"]["price"])
        shares_initial = int(position_info.get("shares", 1))
        risk_dollars = float(position_info.get("risk_dollars", 0.0))
        stop_price = float(plan["stop_loss"]["price"])
        targets = plan.get("targets", {})

        # Allocate shares to each exit tranche (30 / 40 / remainder)
        t1_shares = max(1, int(round(shares_initial * 0.30)))
        t2_shares = max(1, int(round(shares_initial * 0.40)))
        t3_shares = max(0, shares_initial - t1_shares - t2_shares)

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
        self._positions[ticker] = pos
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
        pos = self._require(ticker)
        pos.fill_price = fill_price
        pos.shares_initial = shares
        pos.shares_open = shares
        # Recompute tranche sizes against the actual fill qty
        pos.t1_shares = max(1, int(round(shares * 0.30)))
        pos.t2_shares = max(1, int(round(shares * 0.40)))
        pos.t3_shares = max(0, shares - pos.t1_shares - pos.t2_shares)
        pos.status = "active"
        self.save()
        log.info(f"{ticker}: entry filled @ ${fill_price:.2f} x{shares}")

    def mark_stop_placed(self, ticker: str, order_id: str) -> None:
        """Record the Alpaca order ID of the stop-loss order."""
        pos = self._require(ticker)
        pos.stop_order_id = order_id
        self.save()

    def mark_target_placed(self, ticker: str, level: str, order_id: str) -> None:
        """
        Record the order ID for a target limit sell order.

        Args:
            ticker: Ticker symbol.
            level:  One of "t1", "t2", "t3".
            order_id: Alpaca order ID.
        """
        pos = self._require(ticker)
        setattr(pos, f"{level}_order_id", order_id)
        self.save()

    def mark_target_hit(self, ticker: str, level: str) -> None:
        """
        Record that a target exit was filled.

        Reduces shares_open by the tranche size and sets the hit flag.

        Args:
            ticker: Ticker symbol.
            level:  One of "t1", "t2", "t3".
        """
        pos = self._require(ticker)
        sold = int(getattr(pos, f"{level}_shares"))
        pos.shares_open = max(0, pos.shares_open - sold)
        setattr(pos, f"{level}_hit", True)
        self.save()
        log.info(
            f"{ticker}: {level.upper()} hit — {sold} shares sold,"
            f" {pos.shares_open} remaining"
        )

    def mark_closed(self, ticker: str, reason: str) -> None:
        """Mark position as fully closed."""
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
        return self._positions.get(ticker)

    def get_open(self) -> dict[str, LivePosition]:
        """Return all positions with status != 'closed'."""
        return {t: p for t, p in self._positions.items() if p.status != "closed"}

    def trades_today(self) -> int:
        """Count positions that were opened today."""
        today = date.today().isoformat()
        return sum(1 for p in self._positions.values() if p.trades_today_date == today)

    def remove(self, ticker: str) -> None:
        """Permanently remove a position from the tracker."""
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
