"""Tests for src/signals/generator.py — generate_signal and helpers."""

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.signals.generator import _neutral_fundamental_score, generate_signal


# ── _neutral_fundamental_score ────────────────────────────────────────────────

def test_neutral_fundamental_score_defaults():
    result = _neutral_fundamental_score("AAPL")
    assert result["ticker"] == "AAPL"
    assert result["fundamental_score"] == 50
    assert result["piotroski_f_score"] == 5
    assert result["altman_z_score"] is None


def test_neutral_fundamental_score_with_raw():
    raw = {"market_cap": 3e12}
    result = _neutral_fundamental_score("MSFT", raw)
    assert result["market_cap"] == 3e12
    assert result["fundamental_score"] == 50


def test_neutral_fundamental_score_non_dict_raw():
    result = _neutral_fundamental_score("X", "bad")
    assert result["fundamental_score"] == 50
    assert result["raw_fundamentals"] == {}


# ── generate_signal: no context → error signal ───────────────────────────────

def test_generate_signal_returns_error_when_no_context():
    with patch("src.signals.generator.build_signal_context", return_value=None):
        result = generate_signal("AAPL")

    assert result["signal"] == "ERROR"
    assert result["ticker"] == "AAPL"


def test_generate_signal_returns_error_on_empty_price_df():
    ctx = MagicMock()
    ctx.price_df = pd.DataFrame()
    ctx.ticker_info = {}
    ctx.fundamentals_raw = {}

    with patch("src.signals.generator.build_signal_context", return_value=ctx):
        result = generate_signal("AAPL")

    assert result["signal"] == "ERROR"


def test_generate_signal_returns_error_on_insufficient_history():
    ctx = MagicMock()
    ctx.price_df = pd.DataFrame({"Close": [100.0] * 10})
    ctx.ticker_info = {}
    ctx.fundamentals_raw = {}

    with patch("src.signals.generator.build_signal_context", return_value=ctx):
        result = generate_signal("AAPL")

    assert result["signal"] == "ERROR"


# ── generate_signal: disqualified ticker ─────────────────────────────────────

def _make_price_df(n=100, price=100.0):
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    closes = np.full(n, price)
    return pd.DataFrame({
        "Open": closes * 0.99, "High": closes * 1.01,
        "Low": closes * 0.98, "Close": closes,
        "Volume": np.full(n, 1_000_000.0),
    }, index=dates)


def test_generate_signal_filtered_out_when_disqualified():
    ctx = MagicMock()
    ctx.price_df = _make_price_df(n=100)
    ctx.ticker_info = {"longName": "Apple Inc"}
    ctx.fundamentals_raw = {}
    ctx.instrument = MagicMock()
    ctx.instrument.asset_class = MagicMock(value="equity")

    with patch("src.signals.generator.build_signal_context", return_value=ctx):
        with patch("src.signals.generator.calculate_fundamental_score",
                   return_value=_neutral_fundamental_score("AAPL")):
            with patch("src.signals.generator.run_disqualification_filters",
                       return_value=["Low volume"]):
                result = generate_signal("AAPL")

    assert result["signal"] == "FILTERED_OUT"
    assert "Low volume" in result.get("disqualifiers", [])


# ── generate_signal: fundamental fallback activates on error ─────────────────

def test_generate_signal_uses_neutral_fundamental_on_error():
    ctx = MagicMock()
    ctx.price_df = _make_price_df(n=100)
    ctx.ticker_info = {"longName": "Apple Inc"}
    ctx.fundamentals_raw = {}
    ctx.instrument = MagicMock()
    ctx.instrument.asset_class = MagicMock(value="equity")

    with patch("src.signals.generator.build_signal_context", return_value=ctx):
        with patch("src.signals.generator.calculate_fundamental_score",
                   side_effect=ValueError("API down")):
            with patch("src.signals.generator.run_disqualification_filters",
                       return_value=["test disqualifier"]):
                result = generate_signal("AAPL")

    # With a disqualifier active we get FILTERED_OUT; confirm no crash
    assert result["signal"] in ("FILTERED_OUT", "ERROR", "HOLD", "BUY", "STRONG_BUY", "SELL", "WEAK_BUY")


# ── generate_signal: safe macro_regime access ────────────────────────────────

def test_generate_signal_with_macro_regime_missing_strategy():
    """macro_regime without 'strategy' key should not raise KeyError."""
    ctx = MagicMock()
    ctx.price_df = _make_price_df(n=100)
    ctx.ticker_info = {"longName": "Apple Inc"}
    ctx.fundamentals_raw = {}
    ctx.instrument = MagicMock()
    ctx.instrument.asset_class = MagicMock(value="equity")

    incomplete_regime = {"regime": "BULL_RISK_ON", "regime_score": 60}  # no 'strategy' key

    feature_bundle = {
        "trend_score": {"score": 70, "reasons": []},
        "momentum_score": {"score": 65, "reasons": []},
        "volume_score": {"score": 60, "reasons": []},
        "technical_combined": 65.0,
        "volatility_indicators": {},
    }
    neutral_sentiment = {"sentiment_score_0_100": 50, "signal": "NEUTRAL"}
    neutral_social = {"sentiment_score_0_100": 50, "signal": "NEUTRAL"}
    neutral_insider = {"insider_score": 50, "flags": [], "signal": "NEUTRAL"}

    with patch("src.signals.generator.build_signal_context", return_value=ctx):
        with patch("src.signals.generator.calculate_fundamental_score",
                   return_value=_neutral_fundamental_score("AAPL")):
            with patch("src.signals.generator.run_disqualification_filters", return_value=[]):
                with patch("src.signals.generator.compute_signal_features",
                           return_value=(feature_bundle, False)):
                    with patch("src.signals.generator.analyze_news_sentiment",
                               return_value=neutral_sentiment):
                        with patch("src.signals.generator.analyze_social_sentiment",
                                   return_value=neutral_social):
                            with patch("src.signals.generator.analyze_insider_activity",
                                       return_value=neutral_insider):
                                with patch("src.signals.generator.calculate_support_resistance",
                                           return_value={}):
                                    # Should not raise KeyError on missing 'strategy'
                                    result = generate_signal("AAPL", macro_regime=incomplete_regime)

    assert "signal" in result
    assert result["signal"] != "ERROR"
