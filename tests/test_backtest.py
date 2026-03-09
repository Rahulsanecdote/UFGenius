"""Unit tests for the backtesting engine."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest import engine
from src.backtest.engine import _minimum_check, _simulate_ticker, backtest_signal_system


def _frame(
    closes: list[float],
    *,
    entry_flags: list[bool],
    atr: float = 2.0,
    start: str = "2024-01-01",
) -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Close": closes,
            "ATR_14": [atr] * len(closes),
            "entry_signal": entry_flags,
        },
        index=dates,
    )


class TestMinimumCheck:
    def test_passing_strategy(self):
        result = _minimum_check(sharpe=1.8, win_rate=45, profit_factor=2.0, max_drawdown=-10)
        assert result["all_pass"] is True
        assert "✅" in result["verdict"]

    def test_failing_sharpe(self):
        result = _minimum_check(sharpe=0.5, win_rate=45, profit_factor=2.0, max_drawdown=-10)
        assert result["sharpe_ratio_ok"] is False
        assert result["all_pass"] is False
        assert "❌" in result["verdict"]

    def test_failing_win_rate(self):
        result = _minimum_check(sharpe=1.5, win_rate=30, profit_factor=2.0, max_drawdown=-10)
        assert result["win_rate_ok"] is False

    def test_failing_drawdown(self):
        result = _minimum_check(sharpe=1.5, win_rate=45, profit_factor=2.0, max_drawdown=-35)
        assert result["max_drawdown_ok"] is False

    def test_all_fail(self):
        result = _minimum_check(sharpe=0.2, win_rate=20, profit_factor=0.8, max_drawdown=-40)
        assert result["all_pass"] is False


class TestPortfolioAccounting:
    def test_forced_close_at_end_date(self, monkeypatch):
        frame = _frame([100, 101, 102, 103, 104], entry_flags=[True, False, False, False, False], atr=2.0)

        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_args, **_kwargs: frame)

        result = backtest_signal_system(["AAA"], "2024-01-01", "2024-01-05", initial_capital=10_000)
        trades = result["trades"]
        assert trades, "Expected at least one trade"
        assert any(t["exit_reason"] == "FORCE_CLOSE" for t in trades)
        assert all(t["exit_date"] == "2024-01-05" for t in trades if t["exit_reason"] == "FORCE_CLOSE")

    def test_max_concurrent_positions_enforced(self, monkeypatch):
        frame = _frame([100, 101, 102], entry_flags=[True, False, False], atr=1.0)

        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_args, **_kwargs: frame)

        result = backtest_signal_system(
            ["AAA", "BBB", "CCC"],
            "2024-01-01",
            "2024-01-03",
            initial_capital=10_000,
            max_concurrent_positions=1,
        )
        assert result["max_open_positions"] <= 1

    def test_equity_curve_reconciles_to_final_capital(self, monkeypatch):
        frame = _frame([100, 101, 99, 100], entry_flags=[True, False, False, False], atr=1.0)
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_args, **_kwargs: frame)

        result = backtest_signal_system(["AAA"], "2024-01-01", "2024-01-04", initial_capital=10_000)
        equity_curve = result["equity_curve"]
        assert equity_curve, "Expected non-empty equity curve"

        last = equity_curve[-1]
        assert np.isclose(last["portfolio_value"], result["final_capital"])
        assert last["open_positions"] == 0
        assert np.isclose(last["unrealized_pnl"], 0.0)

    def test_true_entry_and_exit_timestamps_present(self, monkeypatch):
        frame = _frame([100, 101, 102, 103], entry_flags=[True, False, False, False], atr=1.0)
        monkeypatch.setattr(engine, "_prepare_ticker_history", lambda *_args, **_kwargs: frame)

        result = backtest_signal_system(["AAA"], "2024-01-01", "2024-01-04", initial_capital=10_000)
        trades = result["trades"]
        assert trades
        for trade in trades:
            assert "entry_date" in trade and trade["entry_date"]
            assert "exit_date" in trade and trade["exit_date"]
            assert trade["entry_date"] <= trade["exit_date"]


class TestSimulateTicker:
    """Integration-level tests using live data when available."""

    @pytest.mark.integration
    def test_returns_list_of_dicts(self):
        trades = _simulate_ticker("AAPL", "2022-01-01", "2022-06-30", capital_slice=5_000)
        assert isinstance(trades, list)

    @pytest.mark.integration
    def test_trades_have_required_keys(self):
        trades = _simulate_ticker("AAPL", "2022-01-01", "2022-06-30", capital_slice=5_000)
        if trades:
            for t in trades:
                assert "ticker" in t
                assert "pnl" in t
                assert "exit_reason" in t

    @pytest.mark.integration
    def test_pnl_bounded(self):
        trades = _simulate_ticker("MSFT", "2022-01-01", "2022-12-31", capital_slice=5_000)
        for t in trades:
            assert np.isfinite(t["pnl"])
