"""Unit tests for the backtesting engine."""

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import _minimum_check, _simulate_ticker


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


class TestSimulateTicker:
    """Integration-level test using real yfinance data (may be slow, marks as integration)."""

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
            # PnL should be finite
            assert np.isfinite(t["pnl"])
