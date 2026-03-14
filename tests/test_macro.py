"""Tests for src/macro/regime.py — regime detection and fallback."""

from unittest.mock import patch

import pandas as pd
import numpy as np
import pytest

from src.macro.regime import (
    REGIME_STRATEGY,
    _fallback_regime,
    detect_market_regime,
)


# ── Fallback regime ───────────────────────────────────────────────────────────

def test_fallback_regime_has_required_keys():
    r = _fallback_regime()
    assert r["regime"] == "NEUTRAL_CHOPPY"
    assert "regime_score" in r
    assert "strategy" in r
    assert "position_size_multiplier" in r["strategy"]


def test_fallback_regime_strategy_structure():
    r = _fallback_regime()
    strat = r["strategy"]
    assert "bias" in strat
    assert "position_size_multiplier" in strat
    assert isinstance(strat["position_size_multiplier"], (int, float))


# ── detect_market_regime wraps exceptions ─────────────────────────────────────

def test_detect_market_regime_returns_fallback_on_exception():
    with patch("src.macro.regime._compute_regime", side_effect=RuntimeError("network down")):
        result = detect_market_regime()

    assert result["regime"] == "NEUTRAL_CHOPPY"
    assert "strategy" in result


# ── Regime strategy map completeness ─────────────────────────────────────────

@pytest.mark.parametrize("regime_name", [
    "BULL_RISK_ON", "MILD_BULL", "NEUTRAL_CHOPPY", "MILD_BEAR", "BEAR_RISK_OFF"
])
def test_all_regime_names_have_strategy(regime_name):
    assert regime_name in REGIME_STRATEGY
    strat = REGIME_STRATEGY[regime_name]
    assert "bias" in strat
    assert "position_size_multiplier" in strat


@pytest.mark.parametrize("regime_name,expected_mult", [
    ("BULL_RISK_ON",   1.0),
    ("MILD_BULL",      0.8),
    ("NEUTRAL_CHOPPY", 0.5),
    ("MILD_BEAR",      0.3),
    ("BEAR_RISK_OFF",  0.0),
])
def test_position_size_multipliers_are_correct(regime_name, expected_mult):
    assert REGIME_STRATEGY[regime_name]["position_size_multiplier"] == expected_mult


# ── _compute_regime with mocked data ─────────────────────────────────────────

def _make_spy_df(n=250, price=500.0, trend="up"):
    """Create a synthetic SPY DataFrame."""
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    if trend == "up":
        # Price well above 200-day SMA
        closes = np.linspace(400, price, n)
    else:
        # Price below where SMA would be
        closes = np.linspace(price, price * 0.7, n)
    return pd.DataFrame({"Close": closes, "Volume": 1e8}, index=dates)


def _make_vix_df(vix_level=14.0, n=60):
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"Close": [vix_level] * n, "Volume": 1e6}, index=dates)


def test_compute_regime_bull_risk_on():
    spy = _make_spy_df(n=250, price=520.0, trend="up")
    vix = _make_vix_df(vix_level=12.0)
    empty = pd.DataFrame()

    with patch("src.macro.regime._download") as mock_dl:
        mock_dl.side_effect = lambda ticker, **kw: (
            spy if ticker == "SPY" else
            vix if ticker == "^VIX" else
            empty
        )
        with patch("src.macro.regime._fetch_ten_year_yield", return_value=None):
            result = detect_market_regime()

    assert result["regime"] in ("BULL_RISK_ON", "MILD_BULL")
    assert "strategy" in result
    assert result["strategy"]["position_size_multiplier"] > 0


def test_compute_regime_bear_risk_off():
    spy = _make_spy_df(n=250, price=300.0, trend="down")
    vix = _make_vix_df(vix_level=40.0)
    empty = pd.DataFrame()

    with patch("src.macro.regime._download") as mock_dl:
        mock_dl.side_effect = lambda ticker, **kw: (
            spy if ticker == "SPY" else
            vix if ticker == "^VIX" else
            empty
        )
        with patch("src.macro.regime._fetch_ten_year_yield", return_value=None):
            result = detect_market_regime()

    assert result["regime"] in ("BEAR_RISK_OFF", "MILD_BEAR")


def test_compute_regime_returns_fallback_when_spy_empty():
    with patch("src.macro.regime._download", return_value=pd.DataFrame()):
        result = detect_market_regime()

    assert result["regime"] == "NEUTRAL_CHOPPY"


def test_fred_yield_absent_does_not_crash():
    spy = _make_spy_df(n=250, price=480.0, trend="up")
    vix = _make_vix_df(vix_level=18.0)
    empty = pd.DataFrame()

    with patch("src.macro.regime._download") as mock_dl:
        mock_dl.side_effect = lambda ticker, **kw: (
            spy if ticker == "SPY" else
            vix if ticker == "^VIX" else
            empty
        )
        with patch("src.macro.regime._fetch_ten_year_yield", return_value=None):
            result = detect_market_regime()

    assert result["ten_yr_yield"] is None
    assert "regime" in result
