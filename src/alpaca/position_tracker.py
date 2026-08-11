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
import math
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _utcnow() -> datetime:
    """Naive UTC 'now', replacing the deprecated ``datetime.utcnow()``.

    Deliberately naive (tz-info stripped): the ledger writes these as ISO
    strings and reads them back with ``fromisoformat``/``_parse_iso`` for the
    RiskGuard loss-limit and cooldown comparisons, all of which operate on
    naive datetimes. A tz-aware value would raise ``TypeError`` there. This is
    a behavioral no-op vs ``utcnow()``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

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

    # Shares the live stop order currently covers (0 when no/unknown stop).
    # Lets the monitor detect an oversized stop after a partial exit without an
    # extra broker poll. Trailing default keeps old records loadable.
    stop_shares: int = 0

    # Decision context + running realized total, for the paper-trading scorecard
    # (P0.4). Trailing defaults keep pre-P0.4 records loadable.
    signal: str = ""            # the signal label that triggered this entry
    composite_score: float = 0.0  # composite score at entry (per-signal attribution)
    realized_pnl: float = 0.0   # running sum of booked exit P&L for this position


def _coerce_daily_entries(raw: dict) -> dict[str, int]:
    """Keep only str->non-negative-int pairs; drop anything unreadable.

    Prevents a corrupt counter value (e.g. ``{"2026-08-04": "x"}``) from raising
    inside ``load()`` and aborting startup.
    """
    clean: dict[str, int] = {}
    for key, value in raw.items():
        try:
            clean[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            log.warning(f"Position store: dropping unreadable daily count {key!r}")
    return clean


def _safe_float(value, default: float = 0.0) -> float:
    """Coerce to a finite float, falling back to ``default`` on garbage/NaN/inf."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _parse_iso(value) -> Optional[datetime]:
    """Parse an ISO timestamp string to a datetime, or None on failure."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coerce_realized(raw) -> list[dict]:
    """Keep only well-formed realized-P&L entries ({ts, pnl[, ticker]})."""
    if not isinstance(raw, list):
        return []
    clean: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("ts")
        try:
            pnl = float(item.get("pnl"))
        except (TypeError, ValueError):
            continue
        # Non-finite P&L would poison every realized_pnl_since() sum (NaN/inf
        # propagate), silently disabling the loss-limit kill switches.
        if not math.isfinite(pnl):
            continue
        if isinstance(ts, str):
            clean.append({"ts": ts, "ticker": str(item.get("ticker", "")), "pnl": pnl})
    return clean


def _coerce_trades(raw) -> list[dict]:
    """Keep only well-formed closed-trade outcome records (P0.4 scorecard).

    A trade needs at minimum a finite ``pnl`` and a close timestamp; other
    fields are best-effort. A malformed record skips only itself.
    """
    if not isinstance(raw, list):
        return []
    clean: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            pnl = float(item.get("pnl"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pnl):
            continue
        if not isinstance(item.get("closed_at"), str):
            continue
        rec = dict(item)
        rec["pnl"] = pnl
        clean.append(rec)
    return clean


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
        # Realized-P&L ledger: [{ts, ticker, pnl}] recorded at each exit. Source
        # of truth for the daily/weekly loss limits and the post-loss cooldown.
        self._realized: list[dict] = []
        # ISO timestamp of the first tracked entry (paper or live), used by the
        # paper_trade_days_required gate.
        self._trading_since: Optional[str] = None
        # Closed-trade outcome ledger: one record per fully-closed FILLED
        # position (P0.4 scorecard). Distinct from `_realized` (per-exit-tranche
        # events): these are per-TRADE round trips, so their metrics line up with
        # the backtest's trade-level metrics. Bounded to the most recent entries.
        self._trades: list[dict] = []
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
                self._daily_entries = {}
                self._realized = []
                self._trading_since = None
                self._trades = []
                return

            # A valid-JSON-but-non-object store (e.g. a list) must fail safe, not
            # raise on .items() outside the read handler and abort startup.
            if not isinstance(data, dict):
                log.error(
                    f"Position store {self._path} is not a JSON object "
                    f"({type(data).__name__}); starting empty"
                )
                self._positions = {}
                self._daily_entries = {}
                self._realized = []
                self._trading_since = None
                self._trades = []
                return

            # v2 wraps positions + daily entry counts; legacy is a flat
            # ticker->record map. Detect v2 by the "positions" object.
            if isinstance(data.get("positions"), dict):
                raw_positions = data["positions"]
                raw_daily = data.get("daily_entries")
                self._daily_entries = (
                    _coerce_daily_entries(raw_daily)
                    if isinstance(raw_daily, dict)
                    else {}
                )
                raw_realized = data.get("realized")
                self._realized = _coerce_realized(raw_realized)
                ts = data.get("trading_since")
                self._trading_since = ts if isinstance(ts, str) else None
                self._trades = _coerce_trades(data.get("trades"))
            else:
                raw_positions = data
                self._daily_entries = {}
                self._realized = []
                self._trading_since = None
                self._trades = []

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
            self._backfill_open_realized_locked()

            # Backfill today's entry count from records (covers legacy stores and
            # keeps the counter >= records opened today), and drop other days so
            # the counter map stays bounded.
            today = date.today().isoformat()
            record_today = sum(
                1 for p in self._positions.values() if p.trades_today_date == today
            )
            count_today = max(self._daily_entries.get(today, 0), record_today)
            self._daily_entries = {today: count_today} if count_today else {}

            log.info(f"Loaded {len(self._positions)} position(s) from {self._path}")

    def _backfill_open_realized_locked(self) -> None:
        """Reconstruct each OPEN position's running realized_pnl from the ledger.

        The per-exit `realized` ledger is the source of truth. Recomputing the
        running total for open positions on load (a) migrates legacy pre-P0.4
        records that predate the `realized_pnl` field — which would otherwise
        load as 0 and under-count the trade at its final close — and (b) is a
        no-op for current records (booked P&L is in `realized` too). Only OPEN
        positions matter: closed ones already recorded their trade outcome. A
        ticker has at most one open position, so its ledger entries at/after
        opened_at belong to that position. Caller holds the lock.
        """
        for pos in self._positions.values():
            if pos.status == "closed":
                continue
            opened = _parse_iso(pos.opened_at)
            if opened is None:
                continue
            total = 0.0
            for e in self._realized:
                if str(e.get("ticker", "")) != pos.ticker:
                    continue
                ts = _parse_iso(e.get("ts"))
                if ts is not None and ts >= opened:
                    total += float(e.get("pnl", 0.0))
            pos.realized_pnl = total

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
                    "realized": list(self._realized),
                    "trading_since": self._trading_since,
                    "trades": list(self._trades),
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
            opened_at=_utcnow().isoformat(),
            status="pending_fill",
            trades_today_date=date.today().isoformat(),
            signal=str(plan.get("signal", "") or ""),
            composite_score=_safe_float(plan.get("composite_score"), 0.0),
        )
        with self._lock:
            # Re-check the duplicate guard INSIDE the critical section so a
            # concurrent add for the same ticker can't overwrite the first
            # record (and double-count the entry).
            if ticker in self._positions and self._positions[ticker].status != "closed":
                raise ValueError(f"Position already tracked for {ticker}")
            self._positions[ticker] = pos
            # Count this as a distinct entry EVENT (survives a same-day re-entry
            # that replaces a closed record for the same ticker).
            today = date.today().isoformat()
            self._daily_entries[today] = self._daily_entries.get(today, 0) + 1
            if self._trading_since is None:
                self._trading_since = _utcnow().isoformat()
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

    def mark_stop_placed(
        self, ticker: str, order_id: Optional[str], shares: int = 0
    ) -> None:
        """Record the stop-loss order id and the share count it covers.

        Pass ``order_id=None`` to record that no protective stop is currently
        live (``stop_shares`` is reset to 0 in that case).
        """
        with self._lock:
            pos = self._require(ticker)
            pos.stop_order_id = order_id
            pos.stop_shares = int(shares) if order_id else 0
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

    def mark_target_hit(
        self,
        ticker: str,
        level: Literal["t1", "t2", "t3"],
        realized_pnl: Optional[float] = None,
    ) -> None:
        """
        Record that a target exit was filled.

        Reduces shares_open by the tranche size and sets the hit flag. When
        ``realized_pnl`` is given, the ledger entry is appended in the SAME
        critical section and persisted by one atomic save, so a crash can't
        leave the position mutated but the P&L unbooked (or vice versa).

        Args:
            ticker: Ticker symbol.
            level:  One of "t1", "t2", "t3".
            realized_pnl: Realized P&L of the exiting tranche, if known.
        """
        with self._lock:
            pos = self._require(ticker)
            sold = int(getattr(pos, f"{level}_shares"))
            pos.shares_open = max(0, pos.shares_open - sold)
            setattr(pos, f"{level}_hit", True)
            remaining = pos.shares_open
            self._append_realized_locked(ticker, realized_pnl)
            self.save()
        log.info(
            f"{ticker}: {level.upper()} hit — {sold} shares sold,"
            f" {remaining} remaining"
        )

    def mark_closed(
        self, ticker: str, reason: str, realized_pnl: Optional[float] = None
    ) -> None:
        """Mark position as fully closed, optionally booking realized P&L
        atomically with the state change (single save)."""
        with self._lock:
            pos = self._require(ticker)
            # Idempotent: a second close of an already-closed record must not
            # re-book realized P&L or append a duplicate trade outcome (which
            # would distort the loss-limit accounting and the scorecard).
            if pos.status == "closed":
                return
            pos.shares_open = 0
            pos.status = "closed"
            self._append_realized_locked(ticker, realized_pnl)
            self._record_trade_outcome_locked(pos, reason)
            self.save()
        log.info(f"{ticker}: position closed (reason={reason})")

    def record_realized(
        self, ticker: str, pnl: float, now: Optional[datetime] = None
    ) -> None:
        """Append a realized-P&L event (positive = gain, negative = loss)."""
        with self._lock:
            if not self._append_realized_locked(ticker, pnl, now=now):
                return
            self.save()
        log.info(f"{ticker}: realized P&L {pnl:+.2f}")

    def _append_realized_locked(
        self,
        ticker: str,
        pnl: Optional[float],
        now: Optional[datetime] = None,
    ) -> bool:
        """Append a ledger entry (caller holds the lock). Rejects non-finite
        values — NaN/inf would poison every realized_pnl_since() sum and
        silently disable the loss-limit kill switches. Returns True if added."""
        if pnl is None:
            return False
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            log.warning(f"{ticker}: dropping unreadable realized P&L {pnl!r}")
            return False
        if not math.isfinite(value):
            log.warning(f"{ticker}: dropping non-finite realized P&L {value!r}")
            return False
        ts = (now or _utcnow()).isoformat()
        self._realized.append({"ts": ts, "ticker": ticker, "pnl": value})
        # Accumulate the running per-trade total so the scorecard can record one
        # trade-level outcome when the position finally closes.
        pos = self._positions.get(ticker)
        if pos is not None:
            pos.realized_pnl += value
        return True

    def _record_trade_outcome_locked(
        self, pos: LivePosition, reason: str, now: Optional[datetime] = None
    ) -> None:
        """Append one closed-TRADE outcome to the scorecard ledger (P0.4).

        Only records positions that were actually FILLED (a real entry→exit
        round trip). Unfilled/expired/cancelled entries never traded, so they
        must not pollute the win-rate / profit-factor / expectancy metrics.
        Caller holds the lock.
        """
        if pos.fill_price is None or pos.shares_initial <= 0:
            return
        basis = float(pos.fill_price) * int(pos.shares_initial)
        pnl = _safe_float(pos.realized_pnl, 0.0)
        return_pct = round(pnl / basis * 100, 4) if basis > 0 else None
        self._trades.append({
            "ticker": pos.ticker,
            "signal": pos.signal,
            "composite_score": pos.composite_score,
            "opened_at": pos.opened_at,
            "closed_at": (now or _utcnow()).isoformat(),
            "shares": int(pos.shares_initial),
            "entry_price": float(pos.fill_price),
            "pnl": pnl,
            "return_pct": return_pct,
            "reason": str(reason),
            # Account mode at close, so the paper graduation scorecard can
            # exclude real-money outcomes — a live loss must not retroactively
            # lower the paper metrics and block later live entries.
            "paper": bool(getattr(config, "ALPACA_PAPER", True)),
        })
        # Bound growth: keep the most recent trades (the scorecard is a rolling
        # performance record, and the store must not grow without limit).
        cap = max(1, int(getattr(config, "PAPER_SCORECARD_MAX_TRADES", 5000)))
        if len(self._trades) > cap:
            self._trades = self._trades[-cap:]

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def get(self, ticker: str) -> Optional[LivePosition]:
        """Return a snapshot of the position, or None if not tracked.

        A copy (not the live record) so a caller reading fields after the lock
        releases can't observe a record another thread is mid-update on.
        """
        with self._lock:
            pos = self._positions.get(ticker)
            return dataclasses.replace(pos) if pos is not None else None

    def get_open(self) -> dict[str, LivePosition]:
        """Return snapshots of all positions with status != 'closed'."""
        with self._lock:
            return {
                t: dataclasses.replace(p)
                for t, p in self._positions.items()
                if p.status != "closed"
            }

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

    def realized_pnl_since(self, since: datetime) -> float:
        """Sum realized P&L recorded at/after `since` (naive-UTC comparison)."""
        with self._lock:
            total = 0.0
            for e in self._realized:
                ts = _parse_iso(e.get("ts"))
                if ts is not None and ts >= since:
                    total += float(e.get("pnl", 0.0))
            return total

    def last_loss_time(self) -> Optional[datetime]:
        """Timestamp of the most recent realized loss (pnl < 0), or None."""
        with self._lock:
            times = [
                _parse_iso(e.get("ts"))
                for e in self._realized
                if float(e.get("pnl", 0.0)) < 0
            ]
            times = [t for t in times if t is not None]
            return max(times) if times else None

    def trading_since(self) -> Optional[datetime]:
        """When trading (paper or live) first began on this store, or None."""
        with self._lock:
            return _parse_iso(self._trading_since)

    def get_trades(self, paper_only: bool = False) -> list[dict]:
        """Return a copy of the closed-trade outcome ledger (P0.4 scorecard).

        ``paper_only`` keeps just paper-account outcomes — what the graduation
        scorecard uses, so real-money trades can't skew it. Records with no
        ``paper`` flag are treated as paper (the ledger before live graduation
        is paper by construction).
        """
        with self._lock:
            trades = [dict(t) for t in self._trades]
        if paper_only:
            trades = [t for t in trades if t.get("paper", True)]
        return trades

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
