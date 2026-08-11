"""
Paper-trading scorecard (upgrade plan P0.4).

Computes the SAME trade-level metrics the backtest validation produces — win
rate, profit factor, expectancy, and a bootstrap `prob_profitable` with
confidence intervals — but from **realized paper trades** (the position
tracker's closed-trade outcome ledger). The point is a like-for-like comparison:
paper performance measured the way the backtest is measured, so you can tell
whether the live paper edge matches the validated backtest edge before risking
real capital.

The `meets_acceptance` verdict drives the P0.4 upgrade to
`paper_trade_days_required`: going live requires the paper scorecard to clear
configured floors (min trades, profit factor, bootstrap prob-profitable,
positive expectancy) — a **performance** gate, not merely a tenure one.

Nothing here assumes profitability. Too few trades, or metrics below the floors,
means `meets_acceptance = False` and the honest outcome is *keep paper trading*.
Reuses `src/backtest/validation.bootstrap_trade_metrics` so the paper and
backtest numbers come from the exact same estimator.
"""

from __future__ import annotations

from typing import Any, Optional

from src.backtest.validation import DEFAULT_SEED, bootstrap_trade_metrics
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)


def _trade_pnls(trades: list[dict]) -> list[float]:
    out: list[float] = []
    for t in trades or []:
        try:
            v = float(t.get("pnl"))
        except (TypeError, ValueError):
            continue
        out.append(v)
    return out


def compute_scorecard(
    trades: list[dict],
    *,
    initial_capital: float,
    n_bootstrap: int = 1000,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Summarize realized paper trades into backtest-comparable metrics + verdict.

    ``trades`` is the position tracker's closed-trade ledger (``get_trades()``).
    Returns headline trade-level metrics, a bootstrap block (``prob_profitable``
    + CIs, from the shared estimator), and an ``acceptance`` block whose
    ``all_pass`` gates the P0.4 live-performance check.
    """
    pnls = _trade_pnls(trades)
    n = len(pnls)

    min_trades = int(config.PAPER_SCORECARD_MIN_TRADES)
    pf_floor = float(config.PAPER_SCORECARD_PROFIT_FACTOR_FLOOR)
    pp_floor = float(config.PAPER_SCORECARD_PROB_PROFITABLE_FLOOR)
    require_pos_exp = bool(config.PAPER_SCORECARD_REQUIRE_POSITIVE_EXPECTANCY)

    if n == 0:
        return {
            "n_trades": 0,
            "note": "no closed paper trades yet",
            "acceptance": {
                "all_pass": False,
                "checks": {"sufficient_trades_ok": False},
                "min_trades": min_trades,
            },
            "summary": (
                "No closed paper trades yet — nothing to score. Keep paper "
                "trading until there are enough closed round trips to measure."
            ),
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # >= 0
    total_pnl = sum(pnls)
    expectancy = total_pnl / n
    win_rate = len(wins) / n * 100.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    total_return_pct = (total_pnl / initial_capital * 100.0) if initial_capital else None

    boot = bootstrap_trade_metrics(pnls, initial_capital, n_resamples=n_bootstrap, seed=seed)
    prob_profitable = boot.get("prob_profitable")

    # Acceptance gate: same shape/spirit as validate_strategy's OOS gate, tuned
    # for paper trades. profit_factor None (no losses yet) passes the PF check —
    # a lossless record is not a reason to withhold; the other gates still apply.
    checks = {
        "sufficient_trades_ok": n >= min_trades,
        "profit_factor_ok": (profit_factor is None) or (profit_factor >= pf_floor),
        "prob_profitable_ok": prob_profitable is not None and prob_profitable >= pp_floor,
        "positive_expectancy_ok": (not require_pos_exp) or (expectancy > 0),
    }
    all_pass = all(checks.values())

    return {
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "total_pnl": round(total_pnl, 2),
        "expectancy_per_trade": round(expectancy, 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "total_return_pct": round(total_return_pct, 3) if total_return_pct is not None else None,
        "bootstrap": boot,
        "prob_profitable": prob_profitable,
        "acceptance": {
            "all_pass": all_pass,
            "checks": checks,
            "min_trades": min_trades,
            "profit_factor_floor": pf_floor,
            "prob_profitable_floor": pp_floor,
            "require_positive_expectancy": require_pos_exp,
        },
        "summary": (
            "PASS: paper scorecard clears the configured floors."
            if all_pass else
            "NOT PASSING: paper scorecard is below the floors (or too few trades). "
            "Keep paper trading — do not enable live trading yet."
        ),
        "disclaimer": (
            "Paper results include no real slippage/fills and are not a guarantee "
            "of live performance. Past performance is not indicative."
        ),
    }


def scorecard_from_tracker(tracker: Any, *, initial_capital: float, **kwargs) -> dict:
    """Convenience: compute the PAPER scorecard from a PositionTracker's ledger.

    Paper-only by construction: this is the graduation scorecard, so real-money
    outcomes are excluded — a live loss must not retroactively lower the paper
    metrics and block later live entries.
    """
    return compute_scorecard(
        tracker.get_trades(paper_only=True), initial_capital=initial_capital, **kwargs
    )


def meets_live_performance_gate(
    tracker: Any, *, initial_capital: float, seed: int = DEFAULT_SEED
) -> tuple[bool, Optional[dict]]:
    """(passes, scorecard) for the P0.4 live-performance gate.

    When the gate is disabled in config, returns (True, None) so only the tenure
    check applies. Uses a modest bootstrap count to stay cheap on the hot path.
    """
    if not config.PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED:
        return True, None
    card = scorecard_from_tracker(
        tracker, initial_capital=initial_capital, n_bootstrap=500, seed=seed
    )
    return bool(card.get("acceptance", {}).get("all_pass")), card
