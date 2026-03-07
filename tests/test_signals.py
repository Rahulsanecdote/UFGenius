"""Unit tests for signal generation, filters, and trade plan logic."""

import numpy as np
import pandas as pd
import pytest

from src.signals.filters import run_disqualification_filters
from src.signals.trade_plan import generate_trade_plan


@pytest.fixture
def sample_df():
    """250-bar OHLCV with a healthy uptrend."""
    np.random.seed(99)
    n = 250
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    price = 100.0
    closes = []
    for _ in range(n):
        price *= 1 + np.random.normal(0.0005, 0.012)
        closes.append(price)
    closes = pd.Series(closes)
    return pd.DataFrame({
        "Open":   closes * 0.998,
        "High":   closes * 1.01,
        "Low":    closes * 0.99,
        "Close":  closes,
        "Volume": np.random.randint(500_000, 3_000_000, n).astype(float),
    }, index=dates)


@pytest.fixture
def penny_df():
    """DataFrame with a penny stock price."""
    np.random.seed(1)
    n = 60
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    closes = pd.Series([0.50] * n)
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.01,
        "Low": closes * 0.99, "Close": closes,
        "Volume": np.random.randint(50_000, 80_000, n).astype(float),
    }, index=dates)


@pytest.fixture
def illiquid_df():
    """DataFrame with illiquid volume."""
    np.random.seed(2)
    n = 60
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    closes = pd.Series([50.0] * n)
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.01,
        "Low": closes * 0.99, "Close": closes,
        "Volume": [50_000] * n,
    }, index=dates)


@pytest.fixture
def mock_signal(sample_df):
    """A mock signal dict resembling generator output."""
    from src.technical.volatility import calculate_volatility_indicators
    from src.technical.support_resistance import calculate_support_resistance

    price = float(sample_df["Close"].iloc[-1])
    vol = calculate_volatility_indicators(sample_df)
    sr  = calculate_support_resistance(sample_df, price)

    return {
        "ticker": "TEST",
        "signal": "BUY",
        "confidence": "HIGH",
        "score": 72.5,
        "current_price": price,
        "reasons": ["Above 200 SMA ✅", "MACD Bullish ✅", "RVOL 2.1x ✅"],
        "disqualifiers": [],
        "volatility": vol,
        "support_resistance": sr,
        "_df": sample_df,
    }


class TestDisqualificationFilters:
    def test_healthy_ticker_no_disqualifiers(self, sample_df):
        fundamental = {"altman_z_score": 3.5}
        result = run_disqualification_filters("TEST", sample_df, fundamental)
        assert result == [], f"Expected no disqualifiers, got: {result}"

    def test_penny_stock_flagged(self, penny_df):
        fundamental = {"altman_z_score": 2.0}
        result = run_disqualification_filters("PENNY", penny_df, fundamental)
        assert any("PENNY_STOCK" in r for r in result)

    def test_illiquid_flagged(self, illiquid_df):
        fundamental = {"altman_z_score": 2.5}
        result = run_disqualification_filters("ILLIQ", illiquid_df, fundamental)
        assert any("ILLIQUID" in r for r in result)

    def test_bankruptcy_risk_flagged(self, sample_df):
        fundamental = {"altman_z_score": 0.5}
        result = run_disqualification_filters("BANKRUPT", sample_df, fundamental)
        assert any("BANKRUPTCY" in r for r in result)

    def test_empty_df_flagged(self):
        result = run_disqualification_filters("NODATA", pd.DataFrame(), {})
        assert len(result) > 0

    def test_chaser_trap_detected(self, sample_df):
        """Simulate a 60% surge in 5 days."""
        df = sample_df.copy()
        original_last = float(df["Close"].iloc[-1])
        df.iloc[-6, df.columns.get_loc("Close")] = original_last / 1.65  # 65% below current
        result = run_disqualification_filters("SURGE", df, {"altman_z_score": 3.0})
        assert any("CHASER_TRAP" in r for r in result)


class TestTradePlan:
    def test_returns_required_keys(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        for key in ["entry", "stop_loss", "targets", "position", "expected_value"]:
            assert key in plan, f"Missing key: {key}"

    def test_stop_below_entry(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        entry = plan["entry"]["price"]
        stop  = plan["stop_loss"]["price"]
        assert stop < entry, f"Stop {stop} should be below entry {entry}"

    def test_targets_above_entry(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        entry = plan["entry"]["price"]
        for label, t in plan["targets"].items():
            assert t["price"] > entry, f"{label} target {t['price']} should be above entry {entry}"

    def test_targets_ascending(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        t1 = plan["targets"]["T1"]["price"]
        t2 = plan["targets"]["T2"]["price"]
        t3 = plan["targets"]["T3"]["price"]
        assert t1 < t2 < t3, f"Targets not ascending: {t1} < {t2} < {t3}"

    def test_position_sizing_respects_1pct_risk(self, mock_signal, sample_df):
        account = 10_000
        plan = generate_trade_plan("TEST", mock_signal, account_size=account, df=sample_df)
        risk_dollars = plan["position"]["risk_dollars"]
        # Risk should be roughly 1% of account ($100), allow some variance
        assert risk_dollars <= account * 0.015, f"Risk ${risk_dollars} exceeds 1.5% limit"

    def test_max_position_capped_at_10pct(self, mock_signal, sample_df):
        account = 10_000
        plan = generate_trade_plan("TEST", mock_signal, account_size=account, df=sample_df)
        pos_pct = plan["position"]["pct_of_account"]
        assert pos_pct <= 11.0, f"Position {pos_pct}% exceeds 10% limit"

    def test_shares_at_least_1(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        assert plan["position"]["shares"] >= 1

    def test_expected_value_positive(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        ev = plan["expected_value"]
        # With 45% win rate and 2.5:1 R:R the EV should be positive
        assert ev > 0, f"Expected positive EV, got {ev}"
