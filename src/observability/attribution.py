"""
Per-signal outcome attribution (upgrade plan P2.3 — observability stack).

The P0.4 trade-outcome ledger (`PositionTracker._trades`) already stamps every
closed round trip with the **signal label** and **composite score** that opened
it. This turns that history into a per-signal-label scorecard — count, win rate,
average return, and total P&L for each of ``STRONG_BUY`` / ``BUY`` / ``WEAK_BUY``
— so an operator can see *which signal grade actually pays off* rather than
trusting the label at face value.

Pure aggregation over a list of trade dicts. No I/O, no order placement; it reads
realized outcomes, it does not gate anything. Nothing here assumes profitability —
a label with a negative expectancy shows exactly that.
"""

from __future__ import annotations

from typing import Optional


def _num(value: object) -> Optional[float]:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def signal_attribution(trades: list[dict]) -> dict:
    """Group closed trades by their opening signal label → outcome stats.

    Each trade dict is expected to carry ``signal`` (the label at entry), ``pnl``,
    and ``return_pct`` (the P0.4 trade-outcome ledger schema). Trades missing a
    finite ``pnl`` are skipped. Returns ``{"by_signal": {...}, "overall": {...}}``.
    """
    by_label: dict[str, list[dict]] = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        if _num(t.get("pnl")) is None:
            continue
        label = str(t.get("signal") or "UNKNOWN").upper()
        by_label.setdefault(label, []).append(t)

    def _stats(rows: list[dict]) -> dict:
        pnls = [_num(r.get("pnl")) for r in rows]
        pnls = [p for p in pnls if p is not None]
        rets = [_num(r.get("return_pct")) for r in rows]
        rets = [r for r in rets if r is not None]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        return {
            "trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n, 4) if n else None,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / n, 2) if n else None,
            "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        }

    by_signal = {label: _stats(rows) for label, rows in sorted(by_label.items())}
    all_rows = [t for rows in by_label.values() for t in rows]
    return {"by_signal": by_signal, "overall": _stats(all_rows)}


def attribution_from_tracker(tracker, paper_only: bool = False) -> dict:
    """Convenience: per-signal attribution from a PositionTracker's trade ledger."""
    try:
        trades = tracker.get_trades(paper_only=paper_only)
    except Exception:
        trades = []
    return signal_attribution(trades)
