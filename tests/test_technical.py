"""Unit tests for technical analysis modules."""

import numpy as np
import pandas as pd
import pytest

from src.technical.momentum import calculate_momentum_indicators, detect_rsi_divergence, score_momentum
from src.technical.support_resistance import calculate_support_resistance
from src.technical.trend import calculate_parabolic_sar, calculate_trend_indicators, score_trend
from src.technical.volatility import calculate_volatility_indicators
from src.technical.volume import calculate_volume_indicators, score_volume


@pytest.fixture
def sample_df():
    """Create synthetic OHLCV DataFrame with 250 bars."""
    np.random.seed(42)
    n = 250
    dates = pd.date_range("2023-01-01", periods=n, freq="B")

    # Simulate a gentle uptrend with noise
    price = 100.0
    closes = []
    for _ in range(n):
        price *= 1 + np.random.normal(0.0005, 0.015)
        closes.append(price)

    closes = pd.Series(closes)
    df = pd.DataFrame({
        "Open":   closes * (1 - np.abs(np.random.normal(0, 0.005, n))),
        "High":   closes * (1 + np.abs(np.random.normal(0, 0.01, n))),
        "Low":    closes * (1 - np.abs(np.random.normal(0, 0.01, n))),
        "Close":  closes,
        "Volume": np.random.randint(500_000, 5_000_000, n).astype(float),
    }, index=dates)
    return df


class TestTrendIndicators:
    def test_returns_expected_keys(self, sample_df):
        ind = calculate_trend_indicators(sample_df)
        assert "SMA_200" in ind
        assert "EMA_20" in ind
        assert "MACD_line" in ind
        assert "MACD_hist" in ind
        assert "VWAP" in ind
        assert "PSAR" in ind

    def test_sma_length_matches_df(self, sample_df):
        ind = calculate_trend_indicators(sample_df)
        assert len(ind["SMA_50"]) == len(sample_df)

    def test_score_returns_valid_range(self, sample_df):
        ind = calculate_trend_indicators(sample_df)
        price = float(sample_df["Close"].iloc[-1])
        result = score_trend(ind, price)
        assert 0 <= result["score"] <= 100
        assert isinstance(result["reasons"], list)

    def test_parabolic_sar_same_length(self, sample_df):
        psar = calculate_parabolic_sar(sample_df)
        assert len(psar) == len(sample_df)

    def test_empty_df_returns_empty_dict(self):
        ind = calculate_trend_indicators(pd.DataFrame())
        assert ind == {}

    def test_score_trend_insufficient_data(self):
        result = score_trend({}, 100.0)
        assert result["score"] == 50  # neutral fallback


class TestMomentumIndicators:
    def test_rsi_range(self, sample_df):
        ind = calculate_momentum_indicators(sample_df)
        rsi = ind["RSI_14"].dropna()
        assert (rsi >= 0).all()
        assert (rsi <= 100).all()

    def test_stochastic_range(self, sample_df):
        ind = calculate_momentum_indicators(sample_df)
        k = ind["STOCH_K"].dropna()
        assert (k >= 0).all()
        assert (k <= 100).all()

    def test_divergence_returns_strings(self, sample_df):
        prices = sample_df["Close"]
        ind = calculate_momentum_indicators(sample_df)
        rsi = ind["RSI_14"]
        div = detect_rsi_divergence(prices, rsi)
        assert set(div.unique()).issubset({"NONE", "BULLISH_DIVERGENCE", "BEARISH_DIVERGENCE"})

    def test_score_momentum_valid(self, sample_df):
        ind = calculate_momentum_indicators(sample_df)
        result = score_momentum(ind)
        assert 0 <= result["score"] <= 100


class TestVolatilityIndicators:
    def test_atr_positive(self, sample_df):
        ind = calculate_volatility_indicators(sample_df)
        atr = ind["ATR_14"].dropna()
        assert (atr > 0).all()

    def test_bollinger_bands_ordering(self, sample_df):
        ind = calculate_volatility_indicators(sample_df)
        upper = ind["BB_UPPER"].dropna()
        lower = ind["BB_LOWER"].dropna()
        assert (upper > lower).all()

    def test_squeeze_is_boolean_series(self, sample_df):
        ind = calculate_volatility_indicators(sample_df)
        sq = ind["SQUEEZE"]
        assert sq.dtype == bool


class TestVolumeIndicators:
    def test_rvol_positive(self, sample_df):
        ind = calculate_volume_indicators(sample_df)
        rvol = ind["RVOL"].dropna()
        assert (rvol > 0).all()

    def test_obv_same_length(self, sample_df):
        ind = calculate_volume_indicators(sample_df)
        assert len(ind["OBV"]) == len(sample_df)

    def test_cmf_range(self, sample_df):
        ind = calculate_volume_indicators(sample_df)
        cmf = ind["CMF"].dropna()
        assert (cmf >= -1).all()
        assert (cmf <= 1).all()

    def test_score_volume_valid(self, sample_df):
        ind = calculate_volume_indicators(sample_df)
        result = score_volume(ind)
        assert 0 <= result["score"] <= 100


class TestSupportResistance:
    def test_returns_all_keys(self, sample_df):
        price = float(sample_df["Close"].iloc[-1])
        result = calculate_support_resistance(sample_df, price)
        assert "pivots" in result
        assert "fibs" in result
        assert "nearest_support" in result
        assert "nearest_resistance" in result

    def test_pivot_pp_in_range(self, sample_df):
        price = float(sample_df["Close"].iloc[-1])
        result = calculate_support_resistance(sample_df, price)
        pp = result["pivots"].get("PP")
        if pp:
            # PP should be within reasonable range of price history
            assert 0 < pp < price * 10

    def test_empty_df_safe(self):
        result = calculate_support_resistance(pd.DataFrame(), 100.0)
        assert result["nearest_support"] is None
        assert result["nearest_resistance"] is None
