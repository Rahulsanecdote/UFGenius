"""
Backtesting engine — simulates the signal system historically.

Metrics computed:
    Sharpe Ratio      = annualised excess return / portfolio std dev   (target > 1.5)
    Sortino Ratio     = excess return / downside deviation             (target > 2.0)
    Max Drawdown      = (Peak - Trough) / Peak × 100                  (target < 20%)
    Calmar Ratio      = Annual Return / |Max Drawdown|                 (target > 2.0)
    Win Rate          = profitable trades / total trades               (target > 40%)
    Profit Factor     = gross profit / gross loss                      (target > 1.5)
    Expected Value    = (win_rate × avg_win) - (loss_rate × avg_loss)  (must be > 0)
"""

from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

from src.data.fetcher import fetch_ohlcv
from src.technical.volatility import calculate_volatility_indicators
from src.utils.logger import get_logger

log = get_logger(__name__)

RISK_FREE_RATE_ANNUAL = 0.05  # 5% annual risk-free rate
ATR_STOP_MULT = 2.0
TARGET_RR = [1.5, 2.5, 4.0]
TARGET_EXIT_PCTS = [0.30, 0.40, 0.30]


def backtest_signal_system(
    tickers: List[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 10_000,
    max_concurrent_positions: int = 3,
) -> dict:
    """
    Simulate the signal system over a historical period.

    Note: This uses simplified signal logic (RSI + SMA crossover as proxy)
    rather than the full multi-dimensional signal to avoid re-downloading
    full historical data for all modules.

    Args:
        tickers:        List of tickers to backtest.
        start_date:     "YYYY-MM-DD" start date.
        end_date:       "YYYY-MM-DD" end date.
        initial_capital: Starting capital in USD.
        max_concurrent_positions: Max positions at once.

    Returns a performance metrics dict.
    """
    log.info(f"Backtesting {len(tickers)} tickers from {start_date} to {end_date}")

    all_trades = []
    portfolio_values = [initial_capital]
    capital = initial_capital

    for ticker in tickers:
        try:
            trades = _simulate_ticker(ticker, start_date, end_date, capital / len(tickers))
            all_trades.extend(trades)
        except Exception as e:
            log.debug(f"{ticker}: backtest error: {e}")
            continue

    if not all_trades:
        return {"error": "No trades simulated — check tickers and date range"}

    # Aggregate portfolio equity curve (simplified: sum all trade PnL chronologically)
    trades_df = pd.DataFrame(all_trades).sort_values("exit_date")

    cumulative_pnl = trades_df["pnl"].cumsum()
    portfolio_values = [initial_capital] + (initial_capital + cumulative_pnl).tolist()
    pv_series = pd.Series(portfolio_values)

    # ── Returns & Ratios ──────────────────────────────────────────────────
    returns     = pv_series.pct_change().dropna()
    final_value = float(pv_series.iloc[-1])

    total_return_pct = (final_value / initial_capital - 1) * 100

    n_days = (
        datetime.strptime(end_date, "%Y-%m-%d") -
        datetime.strptime(start_date, "%Y-%m-%d")
    ).days
    years = max(n_days / 365, 0.01)
    annual_return_pct = ((1 + total_return_pct / 100) ** (1 / years) - 1) * 100

    rf_daily = RISK_FREE_RATE_ANNUAL / 252
    excess   = returns - rf_daily
    sharpe   = float(excess.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

    downside_std = float(returns[returns < 0].std()) if len(returns[returns < 0]) > 0 else 0.01
    sortino = (annual_return_pct / 100 - RISK_FREE_RATE_ANNUAL) / (downside_std * np.sqrt(252))

    peak = pv_series.cummax()
    drawdown = (pv_series - peak) / peak * 100
    max_drawdown = float(drawdown.min())

    calmar = (annual_return_pct / abs(max_drawdown)) if max_drawdown != 0 else 0.0

    # ── Trade Stats ───────────────────────────────────────────────────────
    wins   = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    n      = len(all_trades)

    win_rate     = len(wins) / n * 100 if n > 0 else 0.0
    avg_win      = np.mean([t["pnl"] for t in wins])   if wins   else 0.0
    avg_loss     = np.mean([t["pnl"] for t in losses]) if losses else 0.0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    ev = (
        (len(wins) / n)   * avg_win +
        (len(losses) / n) * avg_loss
    ) if n > 0 else 0.0

    log.info(
        f"Backtest complete: {n} trades, {win_rate:.1f}% win rate, "
        f"Sharpe {sharpe:.2f}, MaxDD {max_drawdown:.1f}%"
    )

    return {
        "period":             f"{start_date} → {end_date}",
        "tickers_tested":     len(tickers),
        "total_return_pct":   round(total_return_pct, 2),
        "annual_return_pct":  round(annual_return_pct, 2),
        "sharpe_ratio":       round(sharpe, 2),
        "sortino_ratio":      round(sortino, 2),
        "max_drawdown_pct":   round(max_drawdown, 2),
        "calmar_ratio":       round(calmar, 2),
        "total_trades":       n,
        "win_rate_pct":       round(win_rate, 1),
        "avg_win":            round(float(avg_win), 2),
        "avg_loss":           round(float(avg_loss), 2),
        "profit_factor":      round(profit_factor, 2),
        "ev_per_trade":       round(float(ev), 2),
        "gross_profit":       round(gross_profit, 2),
        "gross_loss":         round(gross_loss, 2),
        "final_capital":      round(final_value, 2),
        "minimum_acceptance": _minimum_check(sharpe, win_rate, profit_factor, max_drawdown),
    }


def _simulate_ticker(
    ticker: str,
    start_date: str,
    end_date: str,
    capital_slice: float,
) -> List[dict]:
    """
    Simulate trades for a single ticker using simplified RSI+SMA signal.
    Uses ATR-based stops and tiered targets.
    """
    # Download full period + 200 days of warm-up
    warm_start = (
        datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=200)
    ).strftime("%Y-%m-%d")

    df = fetch_ohlcv(ticker, period="max")
    if df.empty or len(df) < 201:
        return []

    # Filter to date range
    df.index = pd.to_datetime(df.index)
    df_test = df.loc[start_date:end_date].copy()
    if len(df_test) < 20:
        return []

    # Pre-compute indicators on full df
    sma50  = df["Close"].rolling(50).mean()
    sma200 = df["Close"].rolling(200).mean()
    delta  = df["Close"].diff()
    gain   = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rsi14  = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(com=13, adjust=False).mean()

    trades = []
    in_trade = False
    entry_price = stop = t1 = t2 = t3 = 0.0

    for idx in df_test.index:
        try:
            price   = float(df_test.loc[idx, "Close"])
            rsi_val = float(rsi14.loc[idx])
            s50     = float(sma50.loc[idx])
            s200    = float(sma200.loc[idx])
            atr_val = float(atr14.loc[idx])
        except Exception:
            continue

        if not in_trade:
            # Entry signal: above both MAs, RSI 45-65, valid ATR
            if (price > s50 > s200 and 45 <= rsi_val <= 65 and atr_val > 0):
                entry_price = price
                stop = entry_price - ATR_STOP_MULT * atr_val
                risk = entry_price - stop
                t1   = entry_price + TARGET_RR[0] * risk
                t2   = entry_price + TARGET_RR[1] * risk
                t3   = entry_price + TARGET_RR[2] * risk
                in_trade = True

        else:
            # Exit logic
            pnl = None
            exit_reason = None

            if price <= stop:
                pnl = (stop - entry_price) * 1.0  # full position loss
                exit_reason = "STOP"
            elif price >= t3:
                pnl = (
                    (t1 - entry_price) * TARGET_EXIT_PCTS[0] +
                    (t2 - entry_price) * TARGET_EXIT_PCTS[1] +
                    (t3 - entry_price) * TARGET_EXIT_PCTS[2]
                )
                exit_reason = "T3"
            elif price >= t2:
                pnl = (
                    (t1 - entry_price) * TARGET_EXIT_PCTS[0] +
                    (t2 - entry_price) * (TARGET_EXIT_PCTS[1] + TARGET_EXIT_PCTS[2])
                )
                exit_reason = "T2"

            if pnl is not None:
                # Scale PnL to capital slice (1% risk rule)
                shares  = max(int((capital_slice * 0.01) / (entry_price - stop)), 1)
                trade_pnl = pnl * shares

                trades.append({
                    "ticker":      ticker,
                    "entry_date":  str(idx),
                    "exit_date":   str(idx),
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(price, 2),
                    "pnl":         round(trade_pnl, 2),
                    "exit_reason": exit_reason,
                })
                in_trade = False

    return trades


def _minimum_check(sharpe, win_rate, profit_factor, max_drawdown) -> dict:
    """Check if backtest meets minimum acceptance criteria."""
    checks = {
        "sharpe_ratio_ok":    sharpe > 1.0,
        "win_rate_ok":        win_rate > 38,
        "profit_factor_ok":   profit_factor > 1.3,
        "max_drawdown_ok":    max_drawdown > -25,
    }
    checks["all_pass"] = all(checks.values())

    if checks["all_pass"]:
        checks["verdict"] = "✅ Meets minimum criteria — paper trade before going live"
    else:
        failed = [k for k, v in checks.items() if not v and k != "all_pass"]
        checks["verdict"] = f"❌ FAILED: {', '.join(failed)} — do NOT use with real money"

    return checks
