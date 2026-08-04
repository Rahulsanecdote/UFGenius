"""Tests for the execution-path earnings-date lookup (audit follow-up).

Plans built from Alpaca metadata carry no days_to_earnings, so the
trade_earnings_week rule always failed open on the execution path. RiskGuard
now does a best-effort provider lookup before giving up.
"""

import time
from unittest.mock import patch

import pytest

import src.utils.config as cfg
from src.alpaca.executor import RiskGuard, _lookup_days_to_earnings
from src.alpaca.position_tracker import PositionTracker


def _plan(days_to_earnings=None):
    plan = {
        "ticker": "AAPL", "signal": "STRONG_BUY",
        "entry": {"type": "LIMIT", "price": 189.40},
        "stop_loss": {"price": 186.35},
        "targets": {"T1": {"price": 191.69}},
        "position": {"shares": 10, "position_value": 1894.0, "risk_dollars": 30.5},
    }
    if days_to_earnings is not None:
        plan["days_to_earnings"] = days_to_earnings
    return plan


def _portfolio():
    return {"total_equity": 50_000.0, "buying_power": 45_000.0, "position_count": 0}


@pytest.fixture
def tracker(tmp_path):
    return PositionTracker(store_path=str(tmp_path / "pos.json"))


def _info_with_earnings_in(days: int) -> dict:
    return {"earningsTimestamp": time.time() + days * 86_400 + 3_600}


def test_lookup_fills_missing_earnings_and_blocks(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    with patch("src.data.fetcher.fetch_ticker_info", return_value=_info_with_earnings_in(3)):
        ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert not ok and "Earnings" in reason


def test_lookup_far_earnings_allows(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    with patch("src.data.fetcher.fetch_ticker_info", return_value=_info_with_earnings_in(30)):
        ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason


def test_lookup_failure_preserves_fail_open(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    with patch("src.data.fetcher.fetch_ticker_info", side_effect=Exception("provider down")):
        ok, reason = RiskGuard().check(_plan(), _portfolio(), tracker)
    assert ok, reason  # unknown stays fail-open, never a hard block


def test_plan_supplied_days_skip_the_lookup(tracker, monkeypatch):
    monkeypatch.setattr(cfg, "SAFETY", {"trade_earnings_week": False})
    with patch("src.data.fetcher.fetch_ticker_info") as mock_fetch:
        ok, reason = RiskGuard().check(_plan(days_to_earnings=20), _portfolio(), tracker)
    assert ok, reason
    mock_fetch.assert_not_called()  # no network when the plan already knows


def test_lookup_helper_returns_none_on_garbage():
    with patch("src.data.fetcher.fetch_ticker_info", return_value={"earningsTimestamp": "junk"}):
        assert _lookup_days_to_earnings("AAPL") is None
