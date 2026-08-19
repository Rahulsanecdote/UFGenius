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


# ── missing inputs are omitted, never defaulted ──────────────────────────────

def _regime_with(vix_df, ten_yr=None):
    spy = _make_spy_df(n=250, price=520.0, trend="up")
    empty = pd.DataFrame()
    with patch("src.macro.regime._download") as mock_dl:
        mock_dl.side_effect = lambda ticker, **kw: (
            spy if ticker == "SPY" else
            vix_df if ticker == "^VIX" else
            empty
        )
        with patch("src.macro.regime._fetch_ten_year_yield", return_value=ten_yr):
            return detect_market_regime()


def test_missing_vix_is_reported_not_invented():
    """An unavailable VIX must not be scored as though it were measured.

    ^VIX is an index: Alpaca's stock API can't serve it, Polygon is skipped for
    "^" symbols, and Yahoo blocks cloud IPs — so a hosted deployment genuinely
    has no VIX. The old default of 20.0 landed in the "Elevated" band, quietly
    costing -10 and rendering "VIX 20.0 — Elevated" as if observed.
    """
    result = _regime_with(pd.DataFrame())
    assert result["vix"] is None
    assert "vix" in result["data_gaps"]
    flags = " ".join(result["flags"])
    assert "VIX unavailable" in flags
    # The fabricated reading must be gone from the user-visible text entirely.
    assert "20.0" not in flags
    assert "Elevated" not in flags


def test_missing_vix_costs_no_score():
    with_vix = _regime_with(_make_vix_df(vix_level=25.0))    # "Elevated": -10
    without = _regime_with(pd.DataFrame())
    # Omitting the component is not the same as scoring it as elevated.
    assert without["regime_score"] == with_vix["regime_score"] + 10


def test_present_vix_still_scores_normally():
    result = _regime_with(_make_vix_df(vix_level=12.0))
    assert result["vix"] == 12.0
    assert "vix" not in result["data_gaps"]
    assert any("Low Fear" in f for f in result["flags"])


def test_missing_ten_year_yield_is_listed_as_a_gap():
    assert "ten_yr_yield" in _regime_with(_make_vix_df(), ten_yr=None)["data_gaps"]
    assert "ten_yr_yield" not in _regime_with(_make_vix_df(), ten_yr=4.0)["data_gaps"]


def test_fallback_regime_declares_every_gap():
    from src.macro.regime import _fallback_regime

    out = _fallback_regime()
    assert set(out["data_gaps"]) == {"spy", "vix", "ten_yr_yield"}
    assert out["vix"] is None
