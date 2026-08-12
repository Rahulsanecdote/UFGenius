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

    def test_unknown_market_cap_is_disqualified(self, sample_df):
        # Conservative shipped behavior: if market cap cannot be verified from
        # any source, the ticker is disqualified rather than traded blind.
        result = run_disqualification_filters(
            "UNKNOWN",
            sample_df,
            {"altman_z_score": 3.0},
            {},
        )
        assert any("UNKNOWN_MARKET_CAP" in r for r in result)


class TestPennyHardRails:
    """Penny mode is a hard-rail profile, NOT a 'remove all protection' switch."""

    @pytest.fixture(autouse=True)
    def _enable_penny(self, monkeypatch):
        # Pin every PENNY_* value the filters read, so config.yaml / env overrides
        # can't change what these tests assert (they validate the filter logic at
        # a KNOWN profile, not the shipped defaults).
        from src.signals import filters as signal_filters
        cfg = signal_filters.config
        monkeypatch.setattr(cfg, "ALLOW_PENNY_STOCKS", True)
        monkeypatch.setattr(cfg, "PENNY_MIN_PRICE", 0.50)
        monkeypatch.setattr(cfg, "PENNY_MAX_PRICE", 10.0)
        monkeypatch.setattr(cfg, "PENNY_MIN_SHARE_VOLUME", 100_000)
        monkeypatch.setattr(cfg, "PENNY_MIN_DOLLAR_VOLUME", 3_000_000.0)
        monkeypatch.setattr(cfg, "PENNY_MIN_MARKET_CAP", 50_000_000.0)
        monkeypatch.setattr(cfg, "PENNY_MAX_5DAY_GAIN_PCT", 30.0)
        monkeypatch.setattr(cfg, "FILTER_BANKRUPTCY_Z", 1.0)

    @staticmethod
    def _df(price, volume, gain_5d=0.0):
        n = 30
        closes = np.full(n, float(price))
        if gain_5d:
            closes[-6] = price / (1 + gain_5d)  # inject a prior-5-day surge
        return pd.DataFrame({
            "Open": closes, "High": closes * 1.01,
            "Low": closes * 0.99, "Close": closes,
            "Volume": np.full(n, float(volume)),
        })

    def test_good_liquid_penny_passes(self):
        # $2.50 x 4M sh = $10M/day, $120M cap, healthy Z, no surge -> tradeable.
        result = run_disqualification_filters(
            "GOODPENNY", self._df(2.50, 4_000_000), {"altman_z_score": 3.0},
            {"market_cap": 120_000_000},
        )
        assert result == []

    def test_thin_dollar_volume_flagged(self):
        # Zero-volume high-priced name (CSXXY-style) — untradeable despite big cap.
        result = run_disqualification_filters(
            "ZEROVOL", self._df(41.88, 0), {"altman_z_score": 3.0},
            {"market_cap": 9_150_000_000},
        )
        assert any("THIN_DOLLAR_VOLUME" in r for r in result)

    def test_nano_cap_still_flagged_in_penny_mode(self):
        # The market-cap floor is $50M, NOT 0 — a $1.4M pump-zone cap is rejected
        # even with huge share volume (OFAL-style).
        result = run_disqualification_filters(
            "NANO", self._df(1.79, 65_000_000), {"altman_z_score": 3.0},
            {"market_cap": 1_390_000},
        )
        assert any("MICRO_CAP" in r for r in result)

    def test_bankruptcy_check_still_on_in_penny_mode(self):
        # Post-bankruptcy tickers (BBBY+-style) must NOT be waved through.
        result = run_disqualification_filters(
            "BANKRUPT", self._df(2.50, 4_000_000), {"altman_z_score": 0.4},
            {"market_cap": 120_000_000},
        )
        assert any("BANKRUPTCY_RISK" in r for r in result)

    def test_above_band_flagged(self):
        # Above the $10 penny band → rejected (keeps the profile in its lane).
        result = run_disqualification_filters(
            "PRICEY", self._df(48.01, 4_000_000), {"altman_z_score": 3.0},
            {"market_cap": 5_940_000_000},
        )
        assert any("ABOVE_PRICE_BAND" in r for r in result)

    def test_sub_penny_floor_enforced(self):
        # Sub-$0.50 is still rejected in penny mode (spreads/manipulation zone).
        result = run_disqualification_filters(
            "SUBPENNY", self._df(0.42, 3_000_000), {"altman_z_score": 3.0},
            {"market_cap": 120_000_000},
        )
        assert any("PENNY_STOCK" in r for r in result)

    def test_chaser_trap_tighter_in_penny_mode(self):
        # A +40% 5-day surge passes the standard 50% gate but fails penny's 30%.
        result = run_disqualification_filters(
            "SURGE", self._df(2.50, 4_000_000, gain_5d=0.40), {"altman_z_score": 3.0},
            {"market_cap": 120_000_000},
        )
        assert any("CHASER_TRAP" in r for r in result)


class TestTradePlan:
    def test_returns_required_keys(self, mock_signal, sample_df):
        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        for key in ["entry", "stop_loss", "targets", "position", "expected_value"]:
            assert key in plan, f"Missing key: {key}"

    def test_quote_as_of_is_capture_time_not_bar_label(self, mock_signal, sample_df):
        # Regression (PR #32 review): quote_as_of must be the observation/build
        # wall-clock, not the daily bar's index label — otherwise every plan
        # built from 1d bars looks hours old and the staleness breaker rejects
        # every entry. A freshly-built plan must be at most seconds old.
        from datetime import datetime, timezone

        plan = generate_trade_plan("TEST", mock_signal, account_size=10_000, df=sample_df)
        as_of = datetime.fromisoformat(plan["quote_as_of"])
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - as_of).total_seconds()
        assert 0 <= age < 60, f"quote_as_of should be ~now, was {age:.0f}s old"

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

    def test_tiny_account_skips_instead_of_oversizing(self, mock_signal, sample_df):
        """A tiny account that can't afford 1 share within risk limits must SKIP,
        not force a 1-share position that breaches max_position_pct (audit C3)."""
        plan = generate_trade_plan("TEST", mock_signal, account_size=10, df=sample_df)
        assert plan.get("skip") is True
        assert "position" not in plan


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
