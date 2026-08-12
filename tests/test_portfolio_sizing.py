"""Tests for volatility/correlation-aware position sizing (roadmap Phase 4)."""

import math

import numpy as np
import pandas as pd
import pytest

from src.portfolio import sizing


# ── periodic_returns ────────────────────────────────────────────────────────
def test_periodic_returns_basic():
    r = sizing.periodic_returns([100, 110, 99])
    assert r == pytest.approx([0.1, -0.1])


def test_periodic_returns_accepts_series_and_drops_nonfinite():
    r = sizing.periodic_returns(pd.Series([100, np.nan, 120, 120]))
    # NaN price dropped, then 100->120 (0.2), 120->120 (0.0)
    assert r == pytest.approx([0.2, 0.0])


def test_periodic_returns_insufficient_is_empty():
    assert sizing.periodic_returns([100]).size == 0
    assert sizing.periodic_returns([]).size == 0


def test_periodic_returns_zero_price_skipped():
    r = sizing.periodic_returns([0, 100, 110])
    # division by the leading 0 is skipped, 100->110 kept
    assert r == pytest.approx([0.1])


# ── annualized_volatility ───────────────────────────────────────────────────
def test_annualized_volatility_scales_with_sqrt_periods():
    rets = [0.01, -0.01, 0.02, -0.02, 0.0]
    daily_sd = float(np.std(rets, ddof=1))
    assert sizing.annualized_volatility(rets, periods_per_year=252) == pytest.approx(
        daily_sd * math.sqrt(252)
    )


def test_annualized_volatility_insufficient_is_nan():
    assert math.isnan(sizing.annualized_volatility([0.01]))
    assert math.isnan(sizing.annualized_volatility([]))


# ── inverse_variance_weights ────────────────────────────────────────────────
def test_inverse_variance_weights_sum_to_one_and_favor_low_variance():
    w = sizing.inverse_variance_weights([0.01, 0.04])
    assert w.sum() == pytest.approx(1.0)
    assert w[0] > w[1]  # lower variance gets more weight
    assert w == pytest.approx([0.8, 0.2])


def test_inverse_variance_weights_accepts_covariance_matrix():
    cov = np.array([[0.01, 0.0], [0.0, 0.04]])
    w = sizing.inverse_variance_weights(cov)
    assert w == pytest.approx([0.8, 0.2])


def test_inverse_variance_weights_all_invalid_is_equal_weight():
    w = sizing.inverse_variance_weights([0.0, -1.0, float("nan")])
    assert w == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_inverse_variance_weights_empty():
    assert sizing.inverse_variance_weights([]).size == 0


# ── volatility_target_weight ────────────────────────────────────────────────
def test_volatility_target_weight_scales_down_high_vol():
    assert sizing.volatility_target_weight(0.30, 0.15) == pytest.approx(0.5)


def test_volatility_target_weight_capped_at_max():
    # low-vol asset would want >1x; capped so we never lever up past base
    assert sizing.volatility_target_weight(0.05, 0.15) == pytest.approx(1.0)


def test_volatility_target_weight_unusable_is_nan():
    assert math.isnan(sizing.volatility_target_weight(0.0, 0.15))
    assert math.isnan(sizing.volatility_target_weight(float("nan"), 0.15))


# ── average_correlation ─────────────────────────────────────────────────────
def test_average_correlation_perfectly_correlated():
    base = np.array([0.01, -0.02, 0.03, -0.01, 0.02, -0.015])
    assert sizing.average_correlation(base, [base]) == pytest.approx(1.0)


def test_average_correlation_anti_correlated():
    base = np.array([0.01, -0.02, 0.03, -0.01, 0.02, -0.015])
    assert sizing.average_correlation(base, [-base]) == pytest.approx(-1.0)


def test_average_correlation_tail_aligns_unequal_lengths():
    base = np.array([0.01, -0.02, 0.03, -0.01, 0.02, -0.015])
    longer = np.concatenate([[0.5, -0.4], base])  # extra leading points
    # tail overlap is `base` vs itself -> 1.0
    assert sizing.average_correlation(base, [longer]) == pytest.approx(1.0)


def test_average_correlation_empty_book_is_nan():
    assert math.isnan(sizing.average_correlation([0.01, 0.02], []))


def test_average_correlation_zero_variance_holding_skipped():
    base = np.array([0.01, -0.02, 0.03, -0.01])
    flat = np.zeros(4)
    assert math.isnan(sizing.average_correlation(base, [flat]))


# ── correlation_scaled_shares ───────────────────────────────────────────────
def test_correlation_scaled_shares_full_penalty_at_corr_one():
    assert sizing.correlation_scaled_shares(100, 1.0, max_penalty=0.5) == 50


def test_correlation_scaled_shares_no_penalty_for_diversifier():
    assert sizing.correlation_scaled_shares(100, -0.3, max_penalty=0.5) == 100


def test_correlation_scaled_shares_unknown_corr_untouched():
    assert sizing.correlation_scaled_shares(100, float("nan")) == 100
    assert sizing.correlation_scaled_shares(100, None) == 100


def test_correlation_scaled_shares_never_negative():
    assert sizing.correlation_scaled_shares(0, 1.0) == 0


# ── suggest_shares ──────────────────────────────────────────────────────────
def test_suggest_shares_most_binding_layer_wins():
    out = sizing.suggest_shares(
        price=100,
        equity=10_000,
        per_share_risk=2.0,
        risk_per_trade=0.01,
        max_position_pct=0.10,
        asset_volatility=0.30,
        target_volatility=0.15,
        avg_correlation=0.8,
        max_correlation_penalty=0.5,
    )
    # risk=50, cap=10, vol_weight=0.5 -> vol=5 (binds); corr penalty 0.4 -> 3
    assert out["shares_by_risk"] == pytest.approx(50.0)
    assert out["shares_by_cap"] == pytest.approx(10.0)
    assert out["vol_target_weight"] == pytest.approx(0.5)
    assert out["suggested_shares"] == 3


def test_suggest_shares_without_vols_skips_overlay():
    out = sizing.suggest_shares(
        price=100,
        equity=10_000,
        per_share_risk=2.0,
        risk_per_trade=0.01,
        max_position_pct=0.10,
    )
    assert out["vol_target_weight"] is None
    assert out["suggested_shares"] == 10  # capital cap binds


def test_suggest_shares_rejects_bad_inputs():
    out = sizing.suggest_shares(
        price=0,
        equity=10_000,
        per_share_risk=2.0,
        risk_per_trade=0.01,
        max_position_pct=0.10,
    )
    assert out["suggested_shares"] == 0
    assert "non-positive" in out["reason"]
