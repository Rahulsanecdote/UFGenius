"""
Intraday backtest harness — replay the deterministic intraday ENTRIES on
historical intraday bars, with no look-ahead and day-trading discipline.

The daily engine (`src/backtest/engine.py`) validates the *daily composite*
strategy; it never touches the intraday entry evaluators, so neither the
opening-range breakout (`src/signals/intraday_signal.py`) nor the sweep-reclaim
reversal (`src/signals/sweep_reclaim.py`) had an out-of-sample check. This module
fills that gap.

How a trade is simulated (no look-ahead, matching the daily engine's discipline):

1. **Decision** — at each bar T the entry evaluator sees only bars up to and
   including T (a bounded trailing window; the evaluator internally slices to T's
   session). Same bars in, same decision out.
2. **Fill** — a signal on bar T fills at bar **T+1's open**, and only when T+1 is
   in the *same session* (a signal on the session's last bar is dropped — it
   can't be entered and exited the same day).
3. **Manage intrabar** — from the fill bar onward the position is managed on each
   bar's High/Low: **stop-priority** (if the bar's low pierced the stop, assume
   the stop filled first, at ``min(open, stop)`` to reflect gap-through), else
   target partials by the bar's high.
4. **Flat by session end** — any shares still open at the last bar of the entry
   session are closed at that bar's close. **No overnight holds** — this is a
   day-trading harness.

Stop geometry mirrors the live plans: the breakout stops an ATR-multiple below
entry; the sweep-reclaim stops just below the swept wick (an absolute level). A
degenerate reclaim whose stop sits at/above the fill is skipped — the same guard
`build_sweep_reclaim_plan` applies.

Costs reuse the daily model (``BACKTEST_COMMISSION_PCT`` + ``BACKTEST_SLIPPAGE_PCT``,
per-side, on every fill). Metrics are trade-based (win rate, expectancy in R,
profit factor) plus a fixed-fractional-risk equity curve. Nothing here assumes
profitability — it is the tool that lets you find out, and it is honest about its
own biases (see ``bias_disclosures``). Intraday history is short and
provider-dependent, so a green verdict here is necessary, not sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.data.fetcher import _INTRADAY_DEFAULT_PERIOD, fetch_intraday
from src.signals.intraday_signal import evaluate_intraday_entry
from src.signals.sweep_reclaim import evaluate_sweep_reclaim
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# US-equities session boundary. fetch_intraday returns naive-UTC timestamps, so a
# post-market bar after ~20:00 ET rolls to the next UTC date; bucketing sessions
# by UTC date would split one trading day (and force an early session-flat).
# Bucket by market-local (ET) date instead.
_MARKET_TZ = ZoneInfo("America/New_York")


def _session_dates(index: pd.DatetimeIndex) -> np.ndarray:
    """Market-local (ET) calendar date for each bar — the session key.

    The fetcher's index is naive UTC; localize to UTC then convert to ET before
    taking the date so extended-hours bars stay in the correct trading session.
    """
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return idx.tz_convert(_MARKET_TZ).date


def _period_for_range(start: Optional[str], interval: str,
                      today: Optional[pd.Timestamp] = None) -> Optional[str]:
    """Derive a lookback period token so the fetch actually spans ``[start, today]``.

    ``fetch_intraday`` defaults to a short recent window per interval (e.g. 5d for
    1m/5m); with an explicit ``--start`` further back, that default would fetch
    only the last few days and ``_restrict`` would discard them — silently
    returning zero trades. Derive a ``"<N>d"`` period covering start→today (small
    buffer) so the requested range is fetched. This cannot exceed the provider's
    own hard intraday cap (yfinance ~7d for 1m, ~60d for 5m), which no period
    beats. ``None`` (no start) keeps the fetcher's per-interval default.
    """
    if not start:
        return _INTRADAY_DEFAULT_PERIOD.get(interval)  # None-safe default passthrough
    if today is None:
        today = pd.Timestamp(pd.Timestamp.utcnow().date())  # naive, normalized to UTC date
    days = (today.normalize() - pd.Timestamp(start).normalize()).days + 2
    return f"{max(2, days)}d"


def _commission_pct() -> float:
    return float(config.BACKTEST_COMMISSION_PCT)


def _slippage_pct() -> float:
    return float(config.BACKTEST_SLIPPAGE_PCT)


# ── entry strategies ──────────────────────────────────────────────────────────
# Each returns a dict describing the stop, or None for "no entry". `stop_abs` is
# an absolute price level (sweep-reclaim's swept-wick stop); `stop_distance` is a
# below-entry distance (breakout's ATR-multiple). Exactly one is set.

def _breakout_decision(window: pd.DataFrame) -> Optional[dict]:
    d = evaluate_intraday_entry(window)
    if not d.get("enter"):
        return None
    atr = (d.get("intraday") or {}).get("atr")
    if atr is None or not np.isfinite(float(atr)) or float(atr) <= 0:
        return None
    return {
        "stop_abs": None,
        "stop_distance": float(atr) * float(config.ATR_STOP_MULTIPLIER),
        "signal": d.get("signal"),
    }


def _sweep_decision(window: pd.DataFrame) -> Optional[dict]:
    d = evaluate_sweep_reclaim(window)
    if not d.get("enter"):
        return None
    stop_abs = (d.get("sweep") or {}).get("stop_hint")
    if stop_abs is None or not np.isfinite(float(stop_abs)):
        return None
    return {"stop_abs": float(stop_abs), "stop_distance": None, "signal": d.get("signal")}


_STRATEGIES: dict[str, Callable[[pd.DataFrame], Optional[dict]]] = {
    "breakout": _breakout_decision,
    "sweep_reclaim": _sweep_decision,
}


@dataclass
class IntradayTrade:
    ticker: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_ref: float          # next-open reference the geometry keys off
    entry_fill: float         # incl. slippage (cost basis)
    stop: float
    r_multiple: float         # realized R (net of costs)
    pct_return: float         # realized % on entry_fill (net of costs)
    exit_reason: str
    hold_bars: int
    signal: Optional[str]

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "entry_ref": round(self.entry_ref, 4),
            "entry_fill": round(self.entry_fill, 4),
            "stop": round(self.stop, 4),
            "r_multiple": round(self.r_multiple, 4),
            "pct_return": round(self.pct_return * 100, 3),
            "exit_reason": self.exit_reason,
            "hold_bars": self.hold_bars,
            "signal": self.signal,
        }


def _target_levels(entry_ref: float, risk: float) -> list[tuple[float, float]]:
    """(price, fraction-of-initial) for each target, in ascending order.

    Reuses the live target geometry (``TARGET_RR_RATIOS`` / ``TARGET_EXIT_PCTS``);
    exit percents are fractions of the initial position and the final target
    closes whatever remains, so the weights always sum to 1.0.
    """
    rr = list(config.TARGET_RR_RATIOS)
    ep = [float(p) / 100.0 for p in config.TARGET_EXIT_PCTS]
    if len(rr) != len(ep) or not rr:
        # Degenerate config → one target that closes the whole position.
        return [(entry_ref + 2.0 * risk, 1.0)]
    return [(entry_ref + float(r) * risk, f) for r, f in zip(rr, ep)]


def _simulate_trade(
    frame: pd.DataFrame,
    e: int,
    session_end: int,
    entry_ref: float,
    stop: float,
) -> tuple[list[tuple[float, float, str]], int]:
    """Manage one position from fill bar ``e`` to ``session_end``.

    Returns ``(exits, exit_idx)`` where ``exits`` is a list of
    ``(price, weight, reason)`` (weights sum to 1.0) and ``exit_idx`` is the bar
    the position finished on. Stop-priority intrabar; forced flat at session end.
    """
    highs = frame["High"].astype(float).to_numpy()
    lows = frame["Low"].astype(float).to_numpy()
    opens = frame["Open"].astype(float).to_numpy()
    closes = frame["Close"].astype(float).to_numpy()

    risk = entry_ref - stop
    targets = _target_levels(entry_ref, risk)
    hit = [False] * len(targets)
    remaining = 1.0
    exits: list[tuple[float, float, str]] = []
    exit_idx = e

    for j in range(e, session_end + 1):
        exit_idx = j
        if remaining <= 1e-9:
            break
        # Stop priority: if this bar's low pierced the stop, assume it filled
        # first — at min(open, stop) so an open gapping BELOW the stop fills worse.
        if lows[j] <= stop:
            exits.append((min(opens[j], stop), remaining, "STOP"))
            remaining = 0.0
            break
        # Otherwise take targets the bar's high reached, in ascending order.
        for k, (level, frac) in enumerate(targets):
            if remaining <= 1e-9:
                break
            if not hit[k] and highs[j] >= level:
                w = remaining if k == len(targets) - 1 else min(frac, remaining)
                exits.append((level, w, f"T{k + 1}"))
                remaining -= w
                hit[k] = True
        if remaining <= 1e-9:
            break

    # Flat by session end — no overnight holds.
    if remaining > 1e-9:
        exits.append((closes[session_end], remaining, "SESSION_CLOSE"))
        exit_idx = session_end

    return exits, exit_idx


def simulate_intraday_ticker(
    df: pd.DataFrame,
    entry: str,
    *,
    ticker: str = "?",
) -> list[IntradayTrade]:
    """Replay one ticker's intraday frame and return its closed day-trades.

    One position per ticker at a time: scanning resumes only after a trade closes,
    so trades never overlap on the same symbol. Never raises on bad data.
    """
    strat = _STRATEGIES.get(entry)
    if strat is None:
        raise ValueError(f"Unknown intraday entry '{entry}'. Available: {sorted(_STRATEGIES)}")
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return []

    n = len(df)
    max_lb = max(2, int(config.INTRADAY_BACKTEST_MAX_LOOKBACK_BARS))
    sessions = _session_dates(df.index)   # ET calendar date per bar (session key)
    # Last bar index of each session (so a trade can be forced flat at its close).
    last_of: dict[Any, int] = {}
    for idx, s in enumerate(sessions):
        last_of[s] = idx
    session_end_of = [last_of[sessions[i]] for i in range(n)]

    opens = df["Open"].astype(float).to_numpy()
    comm, slip = _commission_pct(), _slippage_pct()
    trades: list[IntradayTrade] = []

    i = 0
    while i < n - 1:
        window = df.iloc[max(0, i + 1 - max_lb): i + 1]
        try:
            decision = strat(window)
        except Exception as exc:  # an evaluator hiccup must not abort the sweep
            log.debug(f"{ticker}: intraday decision error at bar {i}: {exc}")
            decision = None
        if decision is None:
            i += 1
            continue
        # Next-open fill, same session only.
        e = i + 1
        if sessions[e] != sessions[i]:
            i += 1
            continue
        entry_ref = float(opens[e])
        if not np.isfinite(entry_ref) or entry_ref <= 0:
            i += 1
            continue
        stop = (
            decision["stop_abs"]
            if decision["stop_abs"] is not None
            else entry_ref - decision["stop_distance"]
        )
        # Degenerate geometry (stop at/above the fill) → skip, mirroring the live
        # build_sweep_reclaim_plan / next-open-below-stop guard.
        if not np.isfinite(stop) or stop >= entry_ref:
            i += 1
            continue

        session_end = session_end_of[e]
        exits, exit_idx = _simulate_trade(df, e, session_end, entry_ref, stop)

        # Per-share P&L on one initial share (share-count independent): buy incl.
        # slippage + commission, exits incl. slippage + commission, weighted.
        buy = entry_ref * (1 + slip)
        entry_comm = buy * comm
        proceeds = sum(w * lvl * (1 - slip) for lvl, w, _ in exits)
        exit_comm = sum(w * lvl * (1 - slip) * comm for lvl, w, _ in exits)
        pnl = proceeds - buy - entry_comm - exit_comm
        risk = entry_ref - stop
        r_multiple = pnl / risk if risk > 0 else 0.0
        pct_return = pnl / buy if buy > 0 else 0.0

        trades.append(
            IntradayTrade(
                ticker=ticker,
                entry_ts=df.index[e],
                exit_ts=df.index[exit_idx],
                entry_ref=entry_ref,
                entry_fill=buy,
                stop=stop,
                r_multiple=r_multiple,
                pct_return=pct_return,
                exit_reason=exits[-1][2] if exits else "NONE",
                hold_bars=exit_idx - e + 1,
                signal=decision.get("signal"),
            )
        )
        # One position per ticker: resume scanning after the trade closes.
        i = exit_idx + 1

    return trades


def _restrict(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    """Slice a (naive-UTC-indexed) intraday frame to the [start, end] date range."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df if df is not None else pd.DataFrame()
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        df = df.copy()
        df.index = idx.tz_convert(None)
    out = df
    if start:
        out = out.loc[out.index >= pd.Timestamp(start)]
    if end:
        # inclusive of the end date's full session
        out = out.loc[out.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return out


def backtest_intraday(
    tickers: Sequence[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    *,
    interval: Optional[str] = None,
    entry: str = "breakout",
    initial_capital: float = 10_000,
    fetch: Optional[Callable[..., pd.DataFrame]] = None,
) -> dict:
    """Backtest an intraday entry across ``tickers`` on historical intraday bars.

    ``entry`` is ``"breakout"`` or ``"sweep_reclaim"``. ``fetch`` is injectable
    (defaults to ``fetch_intraday``) so tests run offline. Returns a metrics dict
    (see ``_compute_intraday_metrics``); ``{"error": ...}`` on a bad request.
    """
    if entry not in _STRATEGIES:
        return {"error": f"Unknown intraday entry '{entry}'. Available: {sorted(_STRATEGIES)}"}
    if not tickers:
        return {"error": "No tickers supplied"}

    fetch = fetch or fetch_intraday
    iv = str(interval or config.INTRADAY_DEFAULT_INTERVAL)
    # Derive the fetch window from --start so a historical range is actually
    # retrieved (the fetcher's default lookback is only a few days), bounded by
    # the provider's own intraday history cap.
    period = _period_for_range(start, iv)
    min_bars = max(1, int(config.INTRADAY_BACKTEST_MIN_FRAME_BARS))

    all_trades: list[IntradayTrade] = []
    tested = 0
    for t in tickers:
        sym = str(t).upper()
        try:
            df = fetch(sym, interval=iv, period=period)
        except Exception as exc:
            log.debug(f"{sym}: intraday fetch error: {exc}")
            continue
        df = _restrict(df, start, end)
        if df is None or df.empty or len(df) < min_bars:
            continue
        tested += 1
        all_trades.extend(simulate_intraday_ticker(df, entry, ticker=sym))

    return _compute_intraday_metrics(
        all_trades,
        initial_capital=float(initial_capital),
        entry=entry,
        interval=iv,
        tickers_tested=tested,
        start=start,
        end=end,
    )


def _max_drawdown_pct(curve: list[float]) -> float:
    arr = np.asarray(curve, dtype=float)
    if arr.size == 0:
        return 0.0
    peak = np.maximum.accumulate(arr)
    dd = np.where(peak > 0, (arr - peak) / peak, 0.0)
    return float(dd.min() * 100)


def _intraday_minimum_check(
    *, total_trades: int, profit_factor: Optional[float],
    expectancy_r: float, max_drawdown_pct: float,
) -> dict:
    min_trades = int(config.INTRADAY_BACKTEST_MIN_TRADES)
    min_pf = float(config.INTRADAY_BACKTEST_MIN_PROFIT_FACTOR)
    # The limit is a magnitude; accept either sign convention so a positive
    # override (e.g. 25.0) doesn't fail every run (drawdown is <= 0).
    max_dd = -abs(float(config.INTRADAY_BACKTEST_MAX_DRAWDOWN_PCT))
    checks = {
        "enough_trades_ok": total_trades >= min_trades,
        # inf profit factor (no losers) trivially clears the bar.
        "profit_factor_ok": (profit_factor is None) or (profit_factor >= min_pf),
        "expectancy_positive_ok": expectancy_r > 0,
        "max_drawdown_ok": max_drawdown_pct > max_dd,
    }
    checks["all_pass"] = all(checks.values())
    if not checks["enough_trades_ok"]:
        checks["verdict"] = (
            f"⚠️ INSUFFICIENT SAMPLE: {total_trades} trades < {min_trades} required — "
            "result is not statistically meaningful. Gather more data before trusting it."
        )
    elif checks["all_pass"]:
        checks["verdict"] = "✅ Meets minimum criteria — paper trade before going live"
    else:
        failed = [k for k, v in checks.items() if not v and k not in ("all_pass",)]
        checks["verdict"] = f"❌ FAILED: {', '.join(failed)} — do NOT use with real money"
    return checks


def _compute_intraday_metrics(
    trades: list[IntradayTrade],
    *,
    initial_capital: float,
    entry: str,
    interval: str,
    tickers_tested: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    n = len(trades)
    period = f"{start or 'earliest'} → {end or 'latest'}"
    base = {
        "entry": entry,
        "interval": interval,
        "period": period,
        "tickers_tested": tickers_tested,
        "total_trades": n,
    }
    if n == 0:
        base["note"] = (
            "No trades simulated — check tickers, date range, and interval availability. "
            "Intraday history is provider-capped (yfinance ~7d for 1m, ~60d for 5m), so a "
            "--start beyond that cap returns no bars."
        )
        base["minimum_acceptance"] = _intraday_minimum_check(
            total_trades=0, profit_factor=None, expectancy_r=0.0, max_drawdown_pct=0.0,
        )
        base["bias_disclosures"] = _bias_disclosures()
        return base

    rs = np.array([t.r_multiple for t in trades], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    win_rate = float(len(wins) / n * 100)
    avg_win_r = float(wins.mean()) if wins.size else 0.0
    avg_loss_r = float(losses.mean()) if losses.size else 0.0
    expectancy_r = float(rs.mean())
    gross_win_r = float(wins.sum())
    gross_loss_r = float(abs(losses.sum()))
    profit_factor = (gross_win_r / gross_loss_r) if gross_loss_r > 0 else None
    avg_pct = float(np.mean([t.pct_return for t in trades]) * 100)
    avg_hold = float(np.mean([t.hold_bars for t in trades]))

    # Fixed-fractional-risk equity curve: risk RISK_PER_TRADE of equity per trade,
    # compounded in exit-time order. Δequity = equity · risk_frac · R. This is
    # sizing-consistent and share-rounding free; it does NOT model concurrent
    # capital across overlapping trades (disclosed).
    ordered = sorted(trades, key=lambda t: t.exit_ts)
    risk_frac = float(config.RISK_PER_TRADE)
    equity = float(initial_capital)
    curve = [equity]
    per_trade_returns: list[float] = []
    for t in ordered:
        r = risk_frac * t.r_multiple
        per_trade_returns.append(r)
        equity *= (1 + r)
        curve.append(equity)
    total_return_pct = (equity / initial_capital - 1) * 100 if initial_capital > 0 else 0.0
    max_dd = _max_drawdown_pct(curve)

    ret = np.array(per_trade_returns, dtype=float)
    # Per-trade Sharpe (mean/std of trade returns) — NOT annualized: intraday
    # trade cadence is irregular, so any √N scaling would be a fabricated number.
    sharpe_per_trade = float(ret.mean() / ret.std()) if ret.std() > 0 else 0.0

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    metrics = {
        **base,
        "win_rate_pct": round(win_rate, 1),
        "expectancy_r": round(expectancy_r, 3),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_pct_return": round(avg_pct, 3),
        "avg_hold_bars": round(avg_hold, 1),
        "sharpe_per_trade": round(sharpe_per_trade, 3),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "final_capital": round(equity, 2),
        "exit_breakdown": reasons,
        "cost_model": {
            "commission_pct": _commission_pct(),
            "slippage_pct": _slippage_pct(),
            "risk_per_trade": risk_frac,
            "note": "Per-side commission + slippage on every fill; entries fill at "
                    "the NEXT bar's open (same session); stops fill at min(open, "
                    "stop); positions forced flat at session end.",
        },
        "minimum_acceptance": _intraday_minimum_check(
            total_trades=n, profit_factor=profit_factor,
            expectancy_r=expectancy_r, max_drawdown_pct=max_dd,
        ),
        "bias_disclosures": _bias_disclosures(),
        "trades": [t.to_dict() for t in trades],
    }
    return metrics


def _bias_disclosures() -> list[str]:
    return [
        "INTRABAR_ORDERING: within one bar the stop is assumed to fill before any "
        "target (stop-priority). Real sequencing is unknown at bar granularity, so "
        "this is the conservative assumption — finer bars reduce the uncertainty.",
        "NO_CONCURRENCY: each trade is sized independently by fixed-fractional risk; "
        "portfolio interaction across simultaneously-open positions is not modeled.",
        "DATA: intraday history is short and provider-dependent (yfinance caps 1m "
        "at ~7d and 5m at ~60d), and the tested tickers are the supplied list — so "
        "survivorship applies and the window may not cover multiple regimes.",
        "SESSION_FLAT: positions are closed at the session's last bar; a strategy "
        "that would hold overnight is not represented (by design — this is day-trading).",
    ]
