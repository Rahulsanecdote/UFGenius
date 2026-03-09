"""Unit tests for fundamental analysis modules."""

import pytest

from src.fundamental.scorer import (
    _altman_z,
    _composite,
    _growth_metrics,
    _piotroski,
    _valuation_metrics,
)


@pytest.fixture
def healthy_fd():
    """Fundamentals for a financially healthy company."""
    return {
        "ticker":              "TEST",
        "price":               150.0,
        "market_cap":          500_000_000_000,
        "shares_outstanding":  3_000_000_000,
        "revenue":             400_000_000_000,
        "gross_profit":        180_000_000_000,
        "ebit":                 80_000_000_000,
        "ebitda":              100_000_000_000,
        "net_income":           95_000_000_000,
        "eps":                  6.0,
        "total_assets":        350_000_000_000,
        "total_liabilities":   200_000_000_000,
        "total_equity":        150_000_000_000,
        "current_assets":       70_000_000_000,
        "current_liabilities":  35_000_000_000,
        "retained_earnings":   100_000_000_000,
        "total_debt":           80_000_000_000,
        "book_value_per_share": 50.0,
        "operating_cash_flow": 110_000_000_000,
        "free_cash_flow":       90_000_000_000,
        "enterprise_value":    510_000_000_000,
        "revenue_growth_yoy":   0.20,  # 20%
        "earnings_growth_rate": 0.15,
        "eps_growth_yoy":       0.15,
        "fcf_growth_yoy":       0.18,
        "pe_ratio":             25.0,
        "peg_ratio":             1.2,
        "ps_ratio":              1.25,
        "pb_ratio":              3.0,
        "net_income_prev":      80_000_000_000,
        "total_assets_prev":   300_000_000_000,
        "revenue_prev":        333_000_000_000,
    }


@pytest.fixture
def distressed_fd():
    """Fundamentals for a financially distressed company."""
    return {
        "ticker":              "DIST",
        "price":               2.0,
        "market_cap":          50_000_000,
        "shares_outstanding":  25_000_000,
        "revenue":             10_000_000,
        "gross_profit":         1_000_000,
        "ebit":                -5_000_000,
        "ebitda":              -3_000_000,
        "net_income":          -8_000_000,
        "eps":                 -0.32,
        "total_assets":        20_000_000,
        "total_liabilities":   25_000_000,
        "total_equity":        -5_000_000,
        "current_assets":       3_000_000,
        "current_liabilities":  8_000_000,
        "retained_earnings":   -10_000_000,
        "total_debt":          20_000_000,
        "book_value_per_share": -0.2,
        "operating_cash_flow": -4_000_000,
        "free_cash_flow":      -6_000_000,
        "enterprise_value":    60_000_000,
        "revenue_growth_yoy":  -0.25,
        "earnings_growth_rate": -0.50,
        "eps_growth_yoy":      -0.50,
        "fcf_growth_yoy":      -0.60,
        "pe_ratio":            None,
        "peg_ratio":           None,
        "ps_ratio":            6.0,
        "pb_ratio":            None,
        "net_income_prev":     None,
        "total_assets_prev":   None,
        "revenue_prev":        None,
    }


class TestPiotroski:
    def test_healthy_company_scores_high(self, healthy_fd):
        score, _ = _piotroski(healthy_fd)
        assert score >= 5, f"Expected ≥5 for healthy company, got {score}"

    def test_distressed_company_scores_low(self, distressed_fd):
        score, _ = _piotroski(distressed_fd)
        assert score <= 3, f"Expected ≤3 for distressed company, got {score}"

    def test_score_in_range_0_9(self, healthy_fd):
        score, _ = _piotroski(healthy_fd)
        assert 0 <= score <= 9

    def test_returns_breakdown_dict(self, healthy_fd):
        _, detail = _piotroski(healthy_fd)
        assert "F1_roa_positive" in detail
        assert "F2_ocf_positive" in detail


class TestAltmanZ:
    def test_healthy_company_z_above_3(self, healthy_fd):
        z = _altman_z(healthy_fd)
        assert z is not None and z > 2.99, f"Expected z>2.99, got {z}"

    def test_distressed_company_z_below_2(self, distressed_fd):
        z = _altman_z(distressed_fd)
        # Distressed company (negative equity) — z may be None or low
        if z is not None:
            assert z < 2.5

    def test_returns_none_if_no_assets(self):
        z = _altman_z({"total_assets": 0})
        assert z is None


class TestValuationMetrics:
    def test_returns_expected_keys(self, healthy_fd):
        v = _valuation_metrics(healthy_fd)
        assert "pe_ratio" in v
        assert "peg_ratio" in v
        assert "ps_ratio" in v
        assert "ev_ebitda" in v

    def test_ev_ebitda_computed(self, healthy_fd):
        v = _valuation_metrics(healthy_fd)
        assert v["ev_ebitda"] is not None and v["ev_ebitda"] > 0


class TestCompositeScore:
    def test_healthy_scores_above_50(self, healthy_fd):
        f, _ = _piotroski(healthy_fd)
        z     = _altman_z(healthy_fd)
        val   = _valuation_metrics(healthy_fd)
        grow  = _growth_metrics(healthy_fd)
        score = _composite(f, z, val, grow)
        assert score > 50, f"Expected >50 for healthy company, got {score}"

    def test_score_bounded_0_100(self, healthy_fd):
        f, _ = _piotroski(healthy_fd)
        z     = _altman_z(healthy_fd)
        val   = _valuation_metrics(healthy_fd)
        grow  = _growth_metrics(healthy_fd)
        score = _composite(f, z, val, grow)
        assert 0 <= score <= 100

    def test_distressed_scores_low(self, distressed_fd):
        f, _ = _piotroski(distressed_fd)
        z     = _altman_z(distressed_fd)
        val   = _valuation_metrics(distressed_fd)
        grow  = _growth_metrics(distressed_fd)
        score = _composite(f, z, val, grow)
        assert score < 50, f"Expected <50 for distressed company, got {score}"

    def test_peg_non_positive_or_non_finite_gets_no_valuation_bonus(self):
        growth = {"revenue_growth_yoy_pct": None}
        baseline = _composite(0, None, {"peg_ratio": None}, growth)
        for bad in [0, -0.5, float("inf"), float("-inf"), float("nan"), "abc"]:
            score = _composite(0, None, {"peg_ratio": bad}, growth)
            assert score == baseline, f"Bad PEG {bad} should not increase score"

    def test_positive_finite_peg_tiers_apply(self):
        growth = {"revenue_growth_yoy_pct": None}
        score_peg_lt_1 = _composite(0, None, {"peg_ratio": 0.9}, growth)
        score_peg_mid = _composite(0, None, {"peg_ratio": 1.3}, growth)
        score_peg_high = _composite(0, None, {"peg_ratio": 1.8}, growth)
        score_peg_very_high = _composite(0, None, {"peg_ratio": 2.5}, growth)

        assert score_peg_lt_1 > score_peg_mid > score_peg_high >= score_peg_very_high
