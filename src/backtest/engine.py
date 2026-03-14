"""
Backtesting engine — portfolio-level simulation with daily mark-to-market accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from src.data.fetcher import fetch_ohlcv
from src.utils.logger import get_logger

log = get_logger(__name__)

RISK_FREE_RATE_ANNUAL = 0.05
ATR_STOP_MULT = 2.0
TARGET_RR = [1.5, 2.5, 4.0]
TARGET_EXIT_PCTS = [0.30, 0.40, 0.30]
RISK_PER_TRADE = 0.01
MAX_POSITION_PCT = 0.10


@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    shares_initial: int
    shares_open: int
    stop_price: float
    t1: float
    t2: float
    t3: float
    realized_pnl: float = 0.0
    t1_hit: bool = False
    t2_hit: bool = False
    last_price: float | None = None
    last_exit_reason: str | None = None


def backtest_signal_system(
    tickers: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 10_000,
    max_concurrent_positions: int = 3,
) -> dict:
    """
    Simulate strategy over a date range with true entry/exit timestamps and daily MTM equity.
    """
    if not tickers:
        return {"error": "No tickers supplied"}

    if initial_capital <= 0:
        return {"error": "initial_capital must be a positive number"}

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    if end_ts < start_ts:
        return {"error": "end_date must be >= start_date"}

    histories = {
        t.upper(): _prepare_ticker_history(t.upper(), start_ts, end_ts)
        for t in tickers
    }
    histories = {k: v for k, v in histories.items() if not v.empty}
    if not histories:
        return {"error": "No trades simulated — check tickers and date range"}

    calendar = sorted({d for df in histories.values() for d in df.index})
    if not calendar:
        return {"error": "No trades simulated — check tickers and date range"}

    cash = float(initial_capital)
    open_positions: dict[str, Position] = {}
    closed_trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    max_open_positions_seen = 0

    for date in calendar:
        # 1) Manage open positions first (exits/partials), so we can reuse released cash.
        for ticker, pos in list(open_positions.items()):
            frame = histories[ticker]
            if date not in frame.index:
                continue
            close_price = float(frame.loc[date, "Close"])
            pos.last_price = close_price
            cash_ref = [cash]
            _apply_position_exits(pos, close_price, date, closed_trades, cash_ref=cash_ref)
            cash = cash_ref[0]
            if pos.shares_open <= 0:
                open_positions.pop(ticker, None)

        # 2) New entries while respecting max concurrent positions.
        available_slots = max(0, max_concurrent_positions - len(open_positions))
        if available_slots > 0:
            equity_before_entries = cash + _open_positions_market_value(open_positions)
            entry_candidates = _entry_candidates_for_date(histories, open_positions, date)
            for ticker in entry_candidates:
                if available_slots <= 0:
                    break
                frame = histories[ticker]
                row = frame.loc[date]
                entry_price = float(row["Close"])
                atr = float(row["ATR_14"])
                if not np.isfinite(atr) or atr <= 0:
                    continue
                stop_price = entry_price - ATR_STOP_MULT * atr
                shares = _position_size(
                    cash=cash,
                    equity=equity_before_entries,
                    entry_price=entry_price,
                    stop_price=stop_price,
                )
                if shares <= 0:
                    continue
                position_cost = shares * entry_price
                if position_cost > cash:
                    continue
                cash -= position_cost
                risk = entry_price - stop_price
                open_positions[ticker] = Position(
                    ticker=ticker,
                    entry_date=date,
                    entry_price=entry_price,
                    shares_initial=shares,
                    shares_open=shares,
                    stop_price=stop_price,
                    t1=entry_price + TARGET_RR[0] * risk,
                    t2=entry_price + TARGET_RR[1] * risk,
                    t3=entry_price + TARGET_RR[2] * risk,
                    last_price=entry_price,
                )
                available_slots -= 1

        max_open_positions_seen = max(max_open_positions_seen, len(open_positions))
        portfolio_value, unrealized = _portfolio_value(cash, open_positions)
        equity_curve.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "cash": round(cash, 2),
                "unrealized_pnl": round(unrealized, 2),
                "portfolio_value": round(portfolio_value, 2),
                "open_positions": len(open_positions),
            }
        )

    # 3) Force close all positions at end-date.
    force_close_date = end_ts
    for ticker, pos in list(open_positions.items()):
        frame = histories[ticker]
        if frame.empty:
            continue
        if force_close_date in frame.index:
            exit_px = float(frame.loc[force_close_date, "Close"])
        else:
            exit_px = float(frame["Close"].iloc[-1])
        qty = pos.shares_open
        if qty > 0:
            cash += qty * exit_px
            pos.realized_pnl += (exit_px - pos.entry_price) * qty
            pos.shares_open = 0
            pos.last_exit_reason = "FORCE_CLOSE"
        closed_trades.append(
            {
                "ticker": ticker,
                "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                "exit_date": force_close_date.strftime("%Y-%m-%d"),
                "entry_price": round(pos.entry_price, 2),
                "exit_price": round(exit_px, 2),
                "shares": pos.shares_initial,
                "pnl": round(pos.realized_pnl, 2),
                "exit_reason": "FORCE_CLOSE",
            }
        )
        open_positions.pop(ticker, None)

    # Reconcile last equity snapshot after forced closures.
    final_value = round(cash, 2)
    final_date = force_close_date.strftime("%Y-%m-%d")
    if equity_curve and equity_curve[-1]["date"] == final_date:
        equity_curve[-1] = {
            "date": final_date,
            "cash": final_value,
            "unrealized_pnl": 0.0,
            "portfolio_value": final_value,
            "open_positions": 0,
        }
    else:
        equity_curve.append(
            {
                "date": final_date,
                "cash": final_value,
                "unrealized_pnl": 0.0,
                "portfolio_value": final_value,
                "open_positions": 0,
            }
        )

    metrics = _compute_metrics(
        start_date=start_date,
        end_date=end_date,
        initial_capital=float(initial_capital),
        equity_curve=equity_curve,
        trades=closed_trades,
    )
    metrics["tickers_tested"] = len(histories)
    metrics["max_open_positions"] = max_open_positions_seen
    metrics["trades"] = closed_trades
    metrics["equity_curve"] = equity_curve
    return metrics


def _entry_candidates_for_date(
    histories: dict[str, pd.DataFrame],
    open_positions: dict[str, Position],
    date: pd.Timestamp,
) -> list[str]:
    candidates: list[str] = []
    for ticker, frame in histories.items():
        if ticker in open_positions:
            continue
        if date not in frame.index:
            continue
        if bool(frame.loc[date, "entry_signal"]):
            candidates.append(ticker)
    return sorted(candidates)


def _position_size(cash: float, equity: float, entry_price: float, stop_price: float) -> int:
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0
    risk_budget = max(equity, 0.0) * RISK_PER_TRADE
    max_position_value = min(cash, max(equity, 0.0) * MAX_POSITION_PCT)
    shares_by_risk = int(risk_budget / risk_per_share)
    shares_by_cap = int(max_position_value / entry_price) if entry_price > 0 else 0
    return max(min(shares_by_risk, shares_by_cap), 0)


def _apply_position_exits(
    pos: Position,
    close_price: float,
    date: pd.Timestamp,
    closed_trades: list[dict[str, Any]],
    cash_ref: list[float],
) -> None:
    # Stop-loss has priority with daily close data.
    if close_price <= pos.stop_price:
        qty = pos.shares_open
        if qty > 0:
            cash_ref[0] += qty * pos.stop_price
            pos.realized_pnl += (pos.stop_price - pos.entry_price) * qty
            pos.shares_open = 0
            pos.last_exit_reason = "STOP"
    else:
        if (not pos.t1_hit) and close_price >= pos.t1 and pos.shares_open > 0:
            qty = _partial_qty(pos, TARGET_EXIT_PCTS[0])
            if qty > 0:
                cash_ref[0] += qty * pos.t1
                pos.realized_pnl += (pos.t1 - pos.entry_price) * qty
                pos.shares_open -= qty
            pos.t1_hit = True

        if (not pos.t2_hit) and close_price >= pos.t2 and pos.shares_open > 0:
            qty = _partial_qty(pos, TARGET_EXIT_PCTS[1])
            if qty > 0:
                cash_ref[0] += qty * pos.t2
                pos.realized_pnl += (pos.t2 - pos.entry_price) * qty
                pos.shares_open -= qty
            pos.t2_hit = True

        if close_price >= pos.t3 and pos.shares_open > 0:
            qty = pos.shares_open
            cash_ref[0] += qty * pos.t3
            pos.realized_pnl += (pos.t3 - pos.entry_price) * qty
            pos.shares_open = 0
            pos.last_exit_reason = "T3"

    if pos.shares_open == 0:
        exit_price = (
            pos.stop_price
            if pos.last_exit_reason == "STOP"
            else pos.t3
            if pos.last_exit_reason == "T3"
            else close_price
        )
        closed_trades.append(
            {
                "ticker": pos.ticker,
                "entry_date": pos.entry_date.strftime("%Y-%m-%d"),
                "exit_date": date.strftime("%Y-%m-%d"),
                "entry_price": round(pos.entry_price, 2),
                "exit_price": round(float(exit_price), 2),
                "shares": pos.shares_initial,
                "pnl": round(pos.realized_pnl, 2),
                "exit_reason": pos.last_exit_reason or "UNKNOWN",
            }
        )


def _partial_qty(pos: Position, target_fraction: float) -> int:
    target_qty = int(round(pos.shares_initial * target_fraction))
    target_qty = max(target_qty, 1)
    return min(target_qty, pos.shares_open)


def _open_positions_market_value(open_positions: dict[str, Position]) -> float:
    total = 0.0
    for pos in open_positions.values():
        px = pos.last_price if pos.last_price is not None else pos.entry_price
        total += pos.shares_open * px
    return total


def _portfolio_value(cash: float, open_positions: dict[str, Position]) -> tuple[float, float]:
    market_value = 0.0
    unrealized = 0.0
    for pos in open_positions.values():
        px = pos.last_price if pos.last_price is not None else pos.entry_price
        market_value += pos.shares_open * px
        unrealized += (px - pos.entry_price) * pos.shares_open
    return cash + market_value, unrealized


def _prepare_ticker_history(
    ticker: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    warm_start = start_date - timedelta(days=260)
    df = fetch_ohlcv(ticker, period="max")
    if df.empty or len(df) < 220:
        return pd.DataFrame()

    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
    df = df.copy()
    df.index = idx
    df = df.sort_index()
    df = df.loc[(df.index >= warm_start) & (df.index <= end_date)].copy()
    if len(df) < 220:
        return pd.DataFrame()

    sma50 = df["Close"].rolling(50).mean()
    sma200 = df["Close"].rolling(200).mean()
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rsi14 = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = tr.ewm(com=13, adjust=False).mean()

    df["SMA_50"] = sma50
    df["SMA_200"] = sma200
    df["RSI_14"] = rsi14
    df["ATR_14"] = atr14
    df["entry_signal"] = (
        (df["Close"] > df["SMA_50"])
        & (df["SMA_50"] > df["SMA_200"])
        & (df["RSI_14"] >= 45)
        & (df["RSI_14"] <= 65)
        & (df["ATR_14"] > 0)
    ).fillna(False)

    return df.loc[(df.index >= start_date) & (df.index <= end_date)].copy()


def _compute_metrics(
    *,
    start_date: str,
    end_date: str,
    initial_capital: float,
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> dict:
    if not equity_curve:
        return {"error": "No equity curve data"}

    curve_df = pd.DataFrame(equity_curve)
    pv_series = curve_df["portfolio_value"].astype(float)
    returns = pv_series.pct_change().replace([np.inf, -np.inf], np.nan).dropna()

    final_value = float(pv_series.iloc[-1])
    total_return_pct = (final_value / initial_capital - 1) * 100

    n_days = max(
        (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days,
        1,
    )
    years = max(n_days / 365, 1 / 365)
    annual_return_pct = ((final_value / initial_capital) ** (1 / years) - 1) * 100

    if returns.std() > 0:
        rf_daily = RISK_FREE_RATE_ANNUAL / 252
        sharpe = float((returns.mean() - rf_daily) / returns.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    downside = returns[returns < 0]
    downside_std = float(downside.std()) if not downside.empty else 0.0
    if downside_std > 0:
        sortino = float(((returns.mean() - (RISK_FREE_RATE_ANNUAL / 252)) / downside_std) * np.sqrt(252))
    else:
        sortino = 0.0

    peak = pv_series.cummax()
    drawdown = (pv_series - peak) / peak * 100
    max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    calmar = (annual_return_pct / abs(max_drawdown)) if max_drawdown != 0 else 0.0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    win_rate = (len(wins) / n * 100) if n > 0 else 0.0
    avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0.0
    gross_profit = float(sum(t["pnl"] for t in wins))
    gross_loss = float(abs(sum(t["pnl"] for t in losses)))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    ev = (((len(wins) / n) * avg_win) + ((len(losses) / n) * avg_loss)) if n > 0 else 0.0

    return {
        "period": f"{start_date} → {end_date}",
        "total_return_pct": round(total_return_pct, 2),
        "annual_return_pct": round(annual_return_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "calmar_ratio": round(calmar, 2),
        "total_trades": n,
        "win_rate_pct": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else None,  # None = no losing trades
        "ev_per_trade": round(ev, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "final_capital": round(final_value, 2),
        "minimum_acceptance": _minimum_check(sharpe, win_rate, profit_factor, max_drawdown),
    }


def _simulate_ticker(
    ticker: str,
    start_date: str,
    end_date: str,
    capital_slice: float,
) -> list[dict[str, Any]]:
    """
    Backward-compatible single-ticker wrapper used by integration tests.
    """
    result = backtest_signal_system(
        [ticker],
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital_slice,
        max_concurrent_positions=1,
    )
    return result.get("trades", []) if isinstance(result, dict) else []


def _minimum_check(sharpe, win_rate, profit_factor, max_drawdown) -> dict:
    """Check if backtest meets minimum acceptance criteria."""
    checks = {
        "sharpe_ratio_ok": sharpe > 1.0,
        "win_rate_ok": win_rate > 38,
        "profit_factor_ok": profit_factor > 1.3,
        "max_drawdown_ok": max_drawdown > -25,
    }
    checks["all_pass"] = all(checks.values())

    if checks["all_pass"]:
        checks["verdict"] = "✅ Meets minimum criteria — paper trade before going live"
    else:
        failed = [k for k, v in checks.items() if not v and k != "all_pass"]
        checks["verdict"] = f"❌ FAILED: {', '.join(failed)} — do NOT use with real money"

    return checks
