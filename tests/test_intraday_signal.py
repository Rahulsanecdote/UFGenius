"""Tests for the P1.3 intraday entry evaluator and plan builder."""

from __future__ import annotations

import pandas as pd
import pytest

import src.utils.config as cfg
from src.signals.intraday_signal import build_intraday_plan, evaluate_intraday_entry


@pytest.fixture(autouse=True)
def _signal_config(monkeypatch):
    monkeypatch.setattr(cfg, "INTRADAY_MIN_SESSION_BARS", 6)
    monkeypatch.setattr(cfg, "INTRADAY_OPENING_RANGE_MINUTES", 30)
    monkeypatch.setattr(cfg, "INTRADAY_MIN_REL_VOLUME", 1.5)
    monkeypatch.setattr(cfg, "INTRADAY_REQUIRE_ABOVE_VWAP", True)
    monkeypatch.setattr(cfg, "INTRADAY_ATR_PERIOD", 14)


def _frame(bars):
    """bars: list of (price, volume) for one session (09:30 start, 5m)."""
    n = len(bars)
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="5min")
    prices = [b[0] for b in bars]
    vols = [b[1] for b in bars]
    return pd.DataFrame(
        {"Open": prices, "High": [p + 0.15 for p in prices], "Low": [p - 0.15 for p in prices],
         "Close": prices, "Volume": vols},
        index=idx,
    )


def _breakout_session():
    # 6-bar opening range ~100.0-100.5 (vol 1000), quiet middle, final bar breaks
    # to 103 on a volume spike → above VWAP + ORB + volume all fire.
    bars = [(100.0 + i * 0.1, 1000) for i in range(6)]        # OR window
    bars += [(100.6 + (i * 0.05), 1000) for i in range(13)]   # drift up, quiet
    bars += [(103.0, 12000)]                                  # breakout + volume
    return _frame(bars)


def test_strong_buy_on_full_confirmation():
    d = evaluate_intraday_entry(_breakout_session(), now=None)
    assert d["signal"] == "STRONG_BUY" and d["enter"] is True
    tr = d["intraday"]
    assert tr["above_vwap"] and tr["or_breakout"] and tr["volume_ok"]
    assert d["score"] == 100.0


def test_hold_without_volume_participation():
    # Same breakout price but ordinary volume on the last bar → no participation.
    bars = [(100.0 + i * 0.1, 1000) for i in range(6)]
    bars += [(100.6 + i * 0.05, 1000) for i in range(13)]
    bars += [(103.0, 1000)]  # breakout price, but flat volume
    d = evaluate_intraday_entry(_frame(bars), now=None)
    assert d["signal"] == "HOLD" and d["enter"] is False
    assert d["intraday"]["volume_ok"] is False


def test_buy_when_above_vwap_and_volume_but_no_breakout():
    # Price above VWAP with volume, but never exceeds the opening-range high.
    bars = [(100.0 + i * 0.2, 1000) for i in range(6)]     # OR high ~101.0
    bars += [(100.3, 1000) for _ in range(13)]             # below OR high
    bars += [(100.5, 8000)]                                 # volume, still < OR high
    d = evaluate_intraday_entry(_frame(bars), now=None)
    assert d["signal"] == "BUY" and d["enter"] is True
    assert d["intraday"]["or_breakout"] is False


def test_hold_on_insufficient_session():
    d = evaluate_intraday_entry(_frame([(100, 1000)] * 3), now=None)
    assert d["signal"] == "HOLD" and d["enter"] is False


def test_build_plan_from_entry_has_intraday_stop():
    df = _breakout_session()
    d = evaluate_intraday_entry(df, now=None)
    plan = build_intraday_plan("AAA", df, d, account_size=10_000)
    assert plan.get("skip") is not True and "error" not in plan
    entry = plan["entry"]["price"]
    stop = plan["stop_loss"]["price"]
    assert stop < entry                      # protective stop below entry
    assert plan["source"] == "intraday"
    assert "vwap" in plan["intraday"]        # intraday context attached
    assert plan.get("quote_as_of")           # staleness-breaker timestamp stamped


def test_plan_stop_uses_intraday_atr(monkeypatch):
    # The stop distance must equal the intraday ATR × the configured multiplier,
    # proving the decision's intraday ATR (not a recomputed ATR_14) drives it.
    monkeypatch.setattr(cfg, "ATR_STOP_MULTIPLIER", 2.0)
    df = _breakout_session()
    d = evaluate_intraday_entry(df, now=None)
    atr = d["intraday"]["atr"]
    plan = build_intraday_plan("AAA", df, d, account_size=10_000)
    entry = plan["entry"]["price"]
    stop = plan["stop_loss"]["price"]
    assert (entry - stop) == pytest.approx(atr * 2.0, abs=0.02)


def test_build_plan_skips_non_entry():
    plan = build_intraday_plan("AAA", _frame([(100, 1000)] * 3),
                               {"signal": "HOLD", "enter": False}, account_size=10_000)
    assert plan["skip"] is True
