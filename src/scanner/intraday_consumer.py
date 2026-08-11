"""
Intraday candidate consumer (upgrade plan P1.3).

The consumer half of the real-time pipeline: it drains the P1.2 candidate queue,
runs the deterministic intraday entry evaluator (`evaluate_intraday_entry`) on
fresh intraday bars for each candidate, and for confirmed entries builds an
intraday trade plan (intraday-ATR stop) which it hands to a **sink**.

The default sink just logs the plan (dry-run discovery). An execution sink can
route plans through the existing `execute_trade_plan` → RiskGuard path, so the
money-path gating is unchanged — the consumer never bypasses it.

Cost-aware: at most ``INTRADAY_CONSUMER_MAX_PER_CYCLE`` candidates are processed
per drain; the rest stay queued. Per-candidate failures are isolated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from src.data.fetcher import fetch_intraday
from src.data.lookahead import is_stale
from src.scanner.candidate_queue import CandidateQueue
from src.signals.intraday_signal import build_intraday_plan, evaluate_intraday_entry
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IntradayConsumer:
    """Drains a CandidateQueue → intraday entry decision → trade plan → sink."""

    def __init__(
        self,
        queue: CandidateQueue,
        sink: Optional[Callable[[dict], None]] = None,
        fetch: Callable[..., pd.DataFrame] = fetch_intraday,
        account_size: Optional[float] = None,
    ) -> None:
        self.queue = queue
        self.sink = sink or self._log_sink
        self._fetch = fetch
        self.account_size = account_size

    @staticmethod
    def _log_sink(plan: dict) -> None:
        entry = (plan.get("entry") or {}).get("price")
        stop = (plan.get("stop_loss") or {}).get("price")
        log.info(
            f"Intraday entry plan: {plan.get('ticker')} {plan.get('signal')} "
            f"entry={entry} stop={stop} "
            f"vwap={(plan.get('intraday') or {}).get('vwap')}"
        )

    def drain_once(self, now: Optional[datetime] = None) -> list[dict]:
        """Process up to the per-cycle cap of queued candidates. Returns the
        entry plans produced (also passed to the sink)."""
        now = now or _utcnow()
        cap = max(1, int(config.INTRADAY_CONSUMER_MAX_PER_CYCLE))
        candidates = self.queue.drain(max_items=cap)
        interval = config.CONTINUOUS_SCAN_INTERVAL
        max_stale = float(config.INTRADAY_MAX_STALENESS_INTERVALS)
        plans: list[dict] = []
        seen: set[str] = set()
        for c in candidates:
            # Coalesce by ticker: one volume-confirmed breakout queues several
            # candidate kinds (volume/breakout/momentum) for the same symbol —
            # evaluate it once so the sink (and any execution) sees one plan.
            if c.ticker in seen:
                continue
            seen.add(c.ticker)
            try:
                df = self._fetch(c.ticker, interval=interval)
                # Reject stale frames here too: fetch_intraday can return a
                # stale-cache fallback, and generate_trade_plan stamps a fresh
                # quote_as_of that would let a stale plan slip past the P0.3
                # data-staleness circuit breaker.
                if is_stale(df, interval, max_staleness_intervals=max_stale, now=now):
                    log.debug(f"{c.ticker}: intraday frame stale — skipping")
                    continue
                decision = evaluate_intraday_entry(df, now=now)
                if not decision.get("enter"):
                    continue
                plan = build_intraday_plan(c.ticker, df, decision, account_size=self.account_size)
                if isinstance(plan, dict) and "error" not in plan and not plan.get("skip"):
                    self.sink(plan)
                    plans.append(plan)
            except Exception as exc:  # never let one candidate break the drain
                log.debug(f"{c.ticker}: intraday consume error: {exc}")
        if plans:
            log.info(
                f"Intraday consumer: {len(plans)} entry plan(s) from "
                f"{len(candidates)} candidate(s)"
            )
        return plans
