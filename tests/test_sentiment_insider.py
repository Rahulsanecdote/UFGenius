"""Tests for src/sentiment/insider.py — insider activity scoring."""

from unittest.mock import patch

import pytest

from src.sentiment.insider import (
    _safe_float,
    _score_transactions,
    analyze_insider_activity,
)


# ── _safe_float ───────────────────────────────────────────────────────────────

def test_safe_float_valid():
    assert _safe_float("123.45") == 123.45


def test_safe_float_none():
    assert _safe_float(None) == 0.0


def test_safe_float_invalid_string():
    assert _safe_float("not-a-number") == 0.0


def test_safe_float_integer():
    assert _safe_float(100) == 100.0


# ── _score_transactions ───────────────────────────────────────────────────────

def test_empty_transactions_returns_neutral():
    result = _score_transactions([])
    assert result["insider_score"] == 50
    assert result["signal"] == "NEUTRAL"
    assert result["buy_transactions"] == 0
    assert result["sell_transactions"] == 0


def test_large_executive_buy_adds_30():
    txns = [{
        "transaction_type": "P",
        "role": "ceo",
        "shares": 10_000,
        "price": 100.0,
    }]
    result = _score_transactions(txns)
    assert result["insider_score"] >= 80  # 50 + 30
    assert result["signal"] == "BULLISH"
    assert any("Executive bought" in f for f in result["flags"])


def test_small_executive_buy_adds_15():
    txns = [{
        "transaction_type": "P",
        "role": "cfo",
        "shares": 500,
        "price": 250.0,  # value = $125k — above 100k threshold
    }]
    result = _score_transactions(txns)
    assert result["insider_score"] >= 65  # 50 + 15
    assert "Executive bought" in result["flags"][0]


def test_non_executive_buy_adds_10():
    txns = [{
        "transaction_type": "P",
        "role": "vp of engineering",
        "shares": 100,
        "price": 50.0,
    }]
    result = _score_transactions(txns)
    assert result["insider_score"] == 60  # 50 + 10 (single buy)
    assert result["buy_transactions"] == 1


def test_multiple_buys_adds_20():
    txns = [
        {"transaction_type": "P", "role": "vp", "shares": 100, "price": 50.0},
        {"transaction_type": "P", "role": "vp", "shares": 100, "price": 50.0},
        {"transaction_type": "P", "role": "vp", "shares": 100, "price": 50.0},
    ]
    result = _score_transactions(txns)
    # 50 + 20 (multiple buys) + 10 (single buy detection skipped — multiple branch taken)
    assert result["insider_score"] >= 70
    assert any("Multiple insiders buying" in f for f in result["flags"])
    assert result["buy_transactions"] == 3


def test_many_sells_subtracts_10():
    txns = [
        {"transaction_type": "S", "role": "vp", "shares": 100, "price": 50.0}
        for _ in range(5)
    ]
    result = _score_transactions(txns)
    assert result["insider_score"] == 40  # 50 - 10
    assert result["sell_transactions"] == 5
    assert any("Multiple insider sales" in f for f in result["flags"])


def test_bearish_when_net_flow_negative_and_more_sells():
    txns = [
        {"transaction_type": "S", "role": "vp", "shares": 1000, "price": 100.0},
        {"transaction_type": "S", "role": "vp", "shares": 1000, "price": 100.0},
    ]
    result = _score_transactions(txns)
    assert result["signal"] == "BEARISH"
    assert result["net_insider_flow"] < 0


def test_score_clamped_between_0_and_100():
    # Many large exec buys should not exceed 100
    txns = [
        {"transaction_type": "P", "role": "ceo", "shares": 100_000, "price": 200.0}
        for _ in range(10)
    ]
    result = _score_transactions(txns)
    assert 0 <= result["insider_score"] <= 100


def test_result_has_required_keys():
    result = _score_transactions([])
    for key in ("insider_score", "net_insider_flow", "buy_transactions",
                "sell_transactions", "flags", "signal"):
        assert key in result


# ── analyze_insider_activity ──────────────────────────────────────────────────

def test_returns_neutral_on_fetch_exception():
    with patch("src.sentiment.insider._fetch_form4", side_effect=Exception("network")):
        result = analyze_insider_activity("AAPL")
    assert result["signal"] == "NEUTRAL"
    assert result["insider_score"] == 50


def test_returns_neutral_on_empty_edgar_response():
    with patch("src.sentiment.insider._fetch_form4", return_value=[]):
        result = analyze_insider_activity("AAPL")
    assert result["signal"] == "NEUTRAL"


def test_returns_scored_result_with_mocked_transactions():
    txns = [{"transaction_type": "P", "role": "ceo", "shares": 5000, "price": 150.0}]
    with patch("src.sentiment.insider._fetch_form4", return_value=txns):
        result = analyze_insider_activity("AAPL")
    assert result["insider_score"] > 50
    assert result["buy_transactions"] == 1
