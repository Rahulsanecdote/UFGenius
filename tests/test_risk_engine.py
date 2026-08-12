"""Tests for the portfolio-level risk engine (roadmap Phase 4)."""

import numpy as np
import pytest

from src.risk.engine import (
    PortfolioRiskEngine,
    RiskDecision,
    candidate_from_plan,
    holdings_from_portfolio,
    portfolio_summary,
)


def _engine(**overrides):
    base = dict(
        max_gross_leverage=1.0,
        max_single_weight_pct=20.0,
        max_portfolio_heat_pct=6.0,
        correlation_threshold=0.6,
        max_cluster_weight_pct=35.0,
        max_drawdown_halt_pct=15.0,
        min_return_history=4,
    )
    base.update(overrides)
    return PortfolioRiskEngine(**base)


CORR_A = np.array([0.01, -0.02, 0.015, 0.03, -0.01, 0.02, -0.025, 0.012])


# ── approve ─────────────────────────────────────────────────────────────────
def test_within_limits_approves():
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 40, "price": 150, "shares": 10},
        [{"ticker": "MSFT", "value": 2000, "risk": 50}],
        equity=10_000,
    )
    assert isinstance(d, RiskDecision)
    assert d.action == "approve"
    assert d.approved is True
    assert d.suggested_shares == 10
    assert d.advisory is True


# ── hard vetoes ─────────────────────────────────────────────────────────────
def test_single_name_weight_veto():
    d = _engine().evaluate(
        {"ticker": "X", "value": 2500, "risk": 40, "price": 100, "shares": 25},
        [],
        equity=10_000,
    )
    assert d.action == "veto"
    assert d.approved is False
    assert d.suggested_shares == 0
    assert any("single-name weight" in r for r in d.reasons)


def test_gross_leverage_veto():
    d = _engine().evaluate(
        {"ticker": "X", "value": 1500, "risk": 30, "price": 100, "shares": 15},
        [{"ticker": "Z", "value": 9000, "risk": 100}],
        equity=10_000,
    )
    assert d.action == "veto"
    assert any("gross leverage" in r for r in d.reasons)


def test_portfolio_heat_veto():
    d = _engine().evaluate(
        {"ticker": "Y", "value": 1000, "risk": 200, "price": 100, "shares": 10},
        [{"ticker": "Z", "value": 1000, "risk": 500}],
        equity=10_000,
    )
    assert d.action == "veto"
    assert any("portfolio heat" in r for r in d.reasons)


def test_drawdown_guardrail_veto():
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1000, "risk": 30, "price": 100, "shares": 10},
        [],
        equity=10_000,
        peak_equity=12_000,  # 16.7% drawdown > 15% halt
    )
    assert d.action == "veto"
    assert any("drawdown" in r for r in d.reasons)


def test_drawdown_disabled_when_zero():
    d = _engine(max_drawdown_halt_pct=0.0).evaluate(
        {"ticker": "AAPL", "value": 1000, "risk": 30, "price": 100, "shares": 10},
        [],
        equity=10_000,
        peak_equity=100_000,  # huge drawdown, but halt disabled
    )
    assert d.action == "approve"


def test_multiple_breaches_collected():
    d = _engine().evaluate(
        {"ticker": "X", "value": 3000, "risk": 400, "price": 100, "shares": 30},
        [{"ticker": "Z", "value": 9000, "risk": 300}],
        equity=10_000,
    )
    assert d.action == "veto"
    assert len(d.reasons) >= 2  # leverage + single-name + heat


# ── correlated-cluster ──────────────────────────────────────────────────────
def test_correlated_cluster_scales_down():
    corr = CORR_A + 0.0003  # near-identical -> corr ~1
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 30, "price": 150, "shares": 10,
         "returns": CORR_A},
        [{"ticker": "MSFT", "value": 2500, "risk": 40, "returns": corr}],
        equity=10_000,
    )
    assert d.action == "scale_down"
    assert d.approved is True
    # cluster 40% > 35%: room = 3500 - 2500 = 1000 -> 1000/150 = 6 shares
    assert d.suggested_shares == 6
    assert set(d.metrics["cluster_tickers"]) == {"AAPL", "MSFT"}


def test_uncorrelated_holding_does_not_cluster():
    anti = -CORR_A
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 30, "price": 150, "shares": 10,
         "returns": CORR_A},
        [{"ticker": "MSFT", "value": 2500, "risk": 40, "returns": anti}],
        equity=10_000,
    )
    assert d.action == "approve"
    assert d.metrics["cluster_weight"] == pytest.approx(0.15)


def test_candidate_without_returns_skips_cluster_inflation():
    corr = CORR_A + 0.0003
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 30, "price": 150, "shares": 10},
        [{"ticker": "MSFT", "value": 2500, "risk": 40, "returns": corr}],
        equity=10_000,
    )
    assert d.action == "approve"  # no history -> cluster = single-name only
    assert d.metrics["cluster_tickers"] == ["AAPL"]


def test_cluster_already_full_vetoes():
    corr = CORR_A + 0.0003
    # existing correlated holding alone already exceeds the 35% cap
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 30, "price": 150, "shares": 10,
         "returns": CORR_A},
        [{"ticker": "MSFT", "value": 4000, "risk": 40, "returns": corr}],
        equity=10_000,
    )
    assert d.action == "veto"
    assert any("no room" in r for r in d.reasons)


# ── fail-open / robustness ──────────────────────────────────────────────────
def test_none_inputs_fail_open_approve():
    d = _engine().evaluate(None, None, equity=10_000)
    assert d.approved is True
    assert d.action == "approve"


def test_zero_equity_skips_checks():
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1000, "risk": 30, "price": 100, "shares": 10},
        [],
        equity=0,
    )
    assert d.approved is True
    assert "equity unavailable" in d.reasons[0]


def test_to_dict_is_json_friendly():
    d = _engine().evaluate(
        {"ticker": "AAPL", "value": 1500, "risk": 40, "price": 150, "shares": 10}, [], 10_000
    )
    payload = d.to_dict()
    assert payload["action"] == "approve"
    assert "metrics" in payload and "gross_leverage" in payload["metrics"]


# ── adapters ────────────────────────────────────────────────────────────────
def test_candidate_from_plan_reads_standard_layout():
    plan = {
        "ticker": "aapl",
        "position": {"position_value": 1500, "risk_dollars": 40, "shares": 10},
        "entry": {"price": 149.7, "reference_price": 150.0},
    }
    cand = candidate_from_plan(plan)
    assert cand == {
        "ticker": "AAPL",
        "value": 1500.0,
        "risk": 40.0,
        "price": 149.7,
        "shares": 10,
        "returns": None,
    }


def test_candidate_from_plan_missing_fields_default_zero():
    cand = candidate_from_plan({"ticker": "X"})
    assert cand["value"] == 0.0 and cand["shares"] == 0 and cand["price"] == 0.0


def test_holdings_from_portfolio_maps_market_value():
    holds = holdings_from_portfolio(
        {"holdings": [{"ticker": "aapl", "market_value": 2500}, {"ticker": "msft", "value": 1000}]}
    )
    assert holds[0] == {"ticker": "AAPL", "value": 2500.0, "risk": 0.0, "returns": None}
    assert holds[1]["value"] == 1000.0


# ── portfolio_summary ───────────────────────────────────────────────────────
def test_portfolio_summary_computes_book_metrics_and_breaches():
    summary = portfolio_summary(
        [{"ticker": "AAPL", "value": 2500, "risk": 60}, {"ticker": "MSFT", "value": 1500, "risk": 30}],
        equity=10_000,
        engine=_engine(),
    )
    assert summary["gross_leverage"] == pytest.approx(0.4)
    assert summary["max_single_weight"] == pytest.approx(0.25)
    assert summary["breaches"]["single_weight"] is True  # 25% > 20% cap
    assert summary["breaches"]["gross_leverage"] is False
    assert summary["holdings"][0]["ticker"] == "AAPL"  # sorted by weight desc


def test_portfolio_summary_empty_book():
    summary = portfolio_summary([], equity=10_000, engine=_engine())
    assert summary["gross_leverage"] == 0.0
    assert summary["position_count"] == 0
    assert summary["holdings"] == []
