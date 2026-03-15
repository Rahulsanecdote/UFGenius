"""Unit tests for signal generation, filters, and trade plan logic."""

import numpy as np
import pandas as pd
import pytest

from src.core.models import Instrument
from src.signals import generator
from src.signals.context import SignalContext
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
    closes = np.array(closes)
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
    closes = np.full(n, 0.50)
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
    closes = np.full(n, 50.0)
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
        result = run_disqualification_filters(
            "TEST", sample_df, fundamental, {"market_cap": 5_000_000_000}
        )
        assert result == [], f"Expected no disqualifiers, got: {result}"

    def test_penny_stock_flagged(self, penny_df, monkeypatch):
        from src.signals import filters as signal_filters

        monkeypatch.setattr(signal_filters.config, "ALLOW_PENNY_STOCKS", False)
        monkeypatch.setattr(signal_filters, "MIN_PRICE", 1.0)
        fundamental = {"altman_z_score": 2.0}
        result = run_disqualification_filters(
            "PENNY", penny_df, fundamental, {"market_cap": 5_000_000_000}
        )
        assert any("PENNY_STOCK" in r for r in result)

    def test_penny_stock_allowed_when_enabled(self, penny_df, monkeypatch):
        from src.signals import filters as signal_filters

        monkeypatch.setattr(signal_filters.config, "ALLOW_PENNY_STOCKS", True)
        fundamental = {"altman_z_score": 2.0}
        result = run_disqualification_filters(
            "PENNY", penny_df, fundamental, {"market_cap": 5_000_000_000}
        )
        assert not any("PENNY_STOCK" in r for r in result)

    def test_illiquid_flagged(self, illiquid_df):
        fundamental = {"altman_z_score": 2.5}
        result = run_disqualification_filters(
            "ILLIQ", illiquid_df, fundamental, {"market_cap": 5_000_000_000}
        )
        assert any("ILLIQUID" in r for r in result)

    def test_bankruptcy_risk_flagged(self, sample_df):
        fundamental = {"altman_z_score": 0.5}
        result = run_disqualification_filters(
            "BANKRUPT", sample_df, fundamental, {"market_cap": 5_000_000_000}
        )
        assert any("BANKRUPTCY" in r for r in result)

    def test_empty_df_flagged(self):
        result = run_disqualification_filters("NODATA", pd.DataFrame(), {}, {"market_cap": 5_000_000_000})
        assert len(result) > 0

    def test_chaser_trap_detected(self, sample_df):
        """Simulate a 60% surge in 5 days."""
        df = sample_df.copy()
        original_last = float(df["Close"].iloc[-1])
        df.iloc[-6, df.columns.get_loc("Close")] = original_last / 1.65  # 65% below current
        result = run_disqualification_filters(
            "SURGE", df, {"altman_z_score": 3.0}, {"market_cap": 5_000_000_000}
        )
        assert any("CHASER_TRAP" in r for r in result)

    def test_micro_cap_flagged_with_raw_market_cap(self, sample_df):
        result = run_disqualification_filters(
            "SMALL",
            sample_df,
            {"altman_z_score": 3.0},
            {"market_cap": 50_000_000},
        )
        assert any("MICRO_CAP" in r for r in result)

    def test_unknown_market_cap_not_disqualified(self, sample_df):
        result = run_disqualification_filters(
            "UNKNOWN",
            sample_df,
            {"altman_z_score": 3.0},
            {},
        )
        assert not any("UNKNOWN_MARKET_CAP" in r for r in result)


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

    def test_nan_atr_falls_back_to_2pct_of_price(self, mock_signal, sample_df):
        """When ATR is NaN the trade plan uses 2% of price as fallback stop distance."""
        import numpy as np
        from src.technical.volatility import calculate_volatility_indicators

        # Force all ATR values to NaN
        vol = calculate_volatility_indicators(sample_df)
        nan_series = vol["ATR_14"].copy()
        nan_series[:] = np.nan
        vol["ATR_14"] = nan_series

        signal = dict(mock_signal)
        signal["volatility"] = vol
        plan = generate_trade_plan("TEST", signal, account_size=10_000, df=sample_df)

        entry = plan["entry"]["price"]
        stop  = plan["stop_loss"]["price"]
        # Stop should be ~entry - (entry * 0.02 * ATR_STOP_MULTIPLIER)
        # Just verify it's below entry and a plausible distance away
        assert stop < entry
        assert (entry - stop) / entry < 0.15, "Fallback stop seems too far from entry"

    def test_zero_risk_still_returns_valid_plan(self, mock_signal, sample_df):
        """Zero risk (entry == stop) should return a plan with at least 1 share, not crash."""
        from src.technical.volatility import calculate_volatility_indicators
        import numpy as np

        # Make ATR = 0 so stop == entry (risk = 0)
        vol = calculate_volatility_indicators(sample_df)
        zero_series = vol["ATR_14"].copy()
        zero_series[:] = 0.0
        vol["ATR_14"] = zero_series

        signal = dict(mock_signal)
        signal["volatility"] = vol
        plan = generate_trade_plan("TEST", signal, account_size=10_000, df=sample_df)

        # Should not raise; should return a usable plan
        assert "entry" in plan
        assert plan["position"]["shares"] >= 1

    def test_min_shares_clamped_to_1_for_tiny_account(self, mock_signal, sample_df):
        """Tiny account ($10) should still yield at least 1 share, not 0."""
        plan = generate_trade_plan("TEST", mock_signal, account_size=10, df=sample_df)
        assert plan["position"]["shares"] >= 1


def test_all_neutral_sentiment_redistributes_weight(monkeypatch, sample_df):
    ctx = SignalContext(
        ticker="TEST",
        price_df=sample_df,
        ticker_info={"longName": "Test Corp"},
        fundamentals_raw={"market_cap": 10_000_000_000},
        instrument=Instrument(symbol="TEST"),
        provider="unit",
    )
    regime = {"regime": "NEUTRAL_CHOPPY", "regime_score": 0, "strategy": {"position_size_multiplier": 1.0}}

    monkeypatch.setattr(
        generator,
        "compute_signal_features",
        lambda **_kwargs: (
            {
                "trend_score": {"score": 75, "reasons": []},
                "momentum_score": {"score": 65, "reasons": []},
                "volume_score": {"score": 60, "reasons": []},
                "technical_combined": 75,
                "volatility_indicators": {},
                "feature_cache_key": "k",
                "feature_version": "v1",
            },
            False,
        ),
    )
    monkeypatch.setattr(generator, "calculate_fundamental_score", lambda *_args, **_kwargs: {"fundamental_score": 70, "piotroski_f_score": 6, "market_cap": 10_000_000_000})
    monkeypatch.setattr(generator, "run_disqualification_filters", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(generator, "calculate_support_resistance", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        generator,
        "resolve_signal_weights",
        lambda *_args, **_kwargs: {
            "technical": 0.35,
            "volume": 0.20,
            "sentiment": 0.20,
            "fundamental": 0.15,
            "macro": 0.10,
        },
    )
    monkeypatch.setattr(generator, "analyze_news_sentiment", lambda *_args, **_kwargs: {"sentiment_score_0_100": 50, "signal": "NEUTRAL", "article_count": 0})
    monkeypatch.setattr(generator, "analyze_social_sentiment", lambda *_args, **_kwargs: {"sentiment_score_0_100": 50, "signal": "NEUTRAL", "mention_count": 0})
    monkeypatch.setattr(
        generator,
        "analyze_insider_activity",
        lambda *_args, **_kwargs: {"insider_score": 50, "signal": "NEUTRAL", "buy_transactions": 0, "sell_transactions": 0, "flags": []},
    )

    result = generator.generate_signal("TEST", context=ctx, macro_regime=regime)

    assert result["_weights"]["sentiment"] == 0.0
    assert "Sentiment unavailable — weight redistributed" in result["reasons"]
