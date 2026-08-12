"""Tests for the screener presets (oversold-bounce, ma-bounce, breakout)."""

from __future__ import annotations

import types

import numpy as np
import pandas as pd
import pytest

from src.screener.features import compute_screen_features
from src.screener.screener import evaluate_preset, screen_ticker, screen_universe


# ── preset definitions pinned here so tests don't depend on config.yaml edits ──
OVERSOLD = {"min_price": 5.0, "rsi_max": 40, "min_rel_volume": 2.0, "require_change_up": True}
MA_BOUNCE = {"require_above_sma": ["20"], "require_below_sma": ["50"],
             "min_avg_volume": 400_000, "min_rel_volume": 1.0}
BREAKOUT = {"require_above_sma": ["20", "50", "200"], "require_new_high_50": True,
            "min_avg_volume": 100_000, "min_roe_pct": 20}


def _feat(**overrides):
    base = {
        "ticker": "X", "price": 10.0, "sma20": 9.0, "sma50": 11.0, "sma200": 12.0,
        "rsi14": 35.0, "avg_volume_20": 500_000, "last_volume": 1_000_000,
        "rel_volume": 2.0, "high_50": 10.0, "is_new_high_50": True,
        "pct_change_1d": 1.0, "market_cap": 5e8, "roe_pct": 25.0,
        "debt_equity": 0.4, "bars": 220,
    }
    base.update(overrides)
    return base


# ── oversold-bounce ──────────────────────────────────────────────────────────
def test_oversold_bounce_passes():
    r = evaluate_preset(OVERSOLD, _feat(price=8.0, rsi14=28, rel_volume=2.5, pct_change_1d=3.0))
    assert r.passed and r.reasons == []


def test_oversold_bounce_rejects_not_oversold():
    r = evaluate_preset(OVERSOLD, _feat(rsi14=60))
    assert not r.passed and any("RSI_ABOVE_MAX" in x for x in r.reasons)


def test_oversold_bounce_rejects_falling_knife():
    # oversold + high volume but still dropping today → not a bounce yet
    r = evaluate_preset(OVERSOLD, _feat(rsi14=25, rel_volume=3.0, pct_change_1d=-4.0))
    assert not r.passed and any("NOT_TURNING_UP" in x for x in r.reasons)


def test_oversold_bounce_rejects_thin_relvol_and_penny():
    r = evaluate_preset(OVERSOLD, _feat(price=3.0, rel_volume=1.1))
    assert any("PRICE_BELOW_MIN" in x for x in r.reasons)
    assert any("REL_VOLUME_LOW" in x for x in r.reasons)


# ── ma-bounce ────────────────────────────────────────────────────────────────
def test_ma_bounce_passes_between_the_mas():
    # price above 20MA, below 50MA
    r = evaluate_preset(MA_BOUNCE, _feat(price=10.0, sma20=9.5, sma50=10.5,
                                         avg_volume_20=500_000, rel_volume=1.2))
    assert r.passed, r.reasons


def test_ma_bounce_rejects_above_the_50():
    r = evaluate_preset(MA_BOUNCE, _feat(price=12.0, sma20=9.5, sma50=10.5))
    assert not r.passed and any("NOT_BELOW_SMA50" in x for x in r.reasons)


def test_ma_bounce_rejects_thin_volume():
    r = evaluate_preset(MA_BOUNCE, _feat(price=10.0, sma20=9.5, sma50=10.5,
                                         avg_volume_20=100_000))
    assert any("AVG_VOLUME_LOW" in x for x in r.reasons)


# ── breakout ─────────────────────────────────────────────────────────────────
def test_breakout_passes_uptrend_new_high():
    r = evaluate_preset(BREAKOUT, _feat(price=30, sma20=28, sma50=25, sma200=20,
                                        is_new_high_50=True, avg_volume_20=200_000,
                                        roe_pct=25))
    assert r.passed, r.reasons


def test_breakout_rejects_below_a_ma():
    r = evaluate_preset(BREAKOUT, _feat(price=30, sma20=28, sma50=25, sma200=31,
                                        is_new_high_50=True, avg_volume_20=200_000))
    assert not r.passed and any("NOT_ABOVE_SMA200" in x for x in r.reasons)


def test_breakout_rejects_not_new_high():
    r = evaluate_preset(BREAKOUT, _feat(price=30, sma20=28, sma50=25, sma200=20,
                                        is_new_high_50=False, avg_volume_20=200_000))
    assert any("NOT_NEW_50D_HIGH" in x for x in r.reasons)


def test_breakout_optional_roe_skipped_when_missing():
    # ROE unavailable → the optional fundamental criterion is skipped, not failed.
    r = evaluate_preset(BREAKOUT, _feat(price=30, sma20=28, sma50=25, sma200=20,
                                        is_new_high_50=True, avg_volume_20=200_000,
                                        roe_pct=None))
    assert r.passed, r.reasons


def test_breakout_rejects_low_roe_when_present():
    r = evaluate_preset(BREAKOUT, _feat(price=30, sma20=28, sma50=25, sma200=20,
                                        is_new_high_50=True, avg_volume_20=200_000,
                                        roe_pct=5))
    assert any("ROE_LOW" in x for x in r.reasons)


# ── insufficient data ────────────────────────────────────────────────────────
def test_missing_technical_field_is_disqualifying():
    r = evaluate_preset(BREAKOUT, _feat(sma200=None))
    assert not r.passed and any("INSUFFICIENT_DATA: sma200" in x for x in r.reasons)


# ── feature extraction ───────────────────────────────────────────────────────
def _ctx(closes, vols, info=None):
    idx = pd.date_range("2023-01-01", periods=len(closes), freq="B")
    df = pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols},
        index=idx,
    )
    return types.SimpleNamespace(ticker="X", price_df=df, fundamentals_raw={}, ticker_info=info or {})


def test_features_computed_on_long_uptrend():
    n = 220
    closes = list(np.linspace(10, 30, n))
    feat = compute_screen_features(_ctx(closes, [300_000] * n, {"returnOnEquity": 0.25}))
    assert feat["price"] == pytest.approx(30.0)
    assert feat["sma20"] and feat["sma50"] and feat["sma200"]
    assert feat["sma20"] > feat["sma200"]          # uptrend: fast MA above slow
    assert feat["is_new_high_50"] is True
    assert feat["roe_pct"] == pytest.approx(25.0)


def test_features_relative_volume_and_change():
    n = 60
    closes = list(np.linspace(20, 8, n - 1)) + [8.4]
    vols = [500_000] * (n - 1) + [1_500_000]
    feat = compute_screen_features(_ctx(closes, vols))
    assert feat["rel_volume"] == pytest.approx(3.0, rel=0.15)  # ~3x its average
    assert feat["pct_change_1d"] > 0
    assert feat["rsi14"] < 40                                   # long decline → oversold


def test_features_none_on_short_history():
    feat = compute_screen_features(_ctx([10, 11, 12], [1000, 1000, 1000]))
    assert feat["sma200"] is None and feat["rsi14"] is None
    assert feat["price"] == pytest.approx(12.0)


def test_features_never_raises_on_empty():
    feat = compute_screen_features(types.SimpleNamespace(ticker="X", price_df=pd.DataFrame()))
    assert feat["price"] is None and feat["bars"] == 0


# ── screen_universe / screen_ticker orchestration ────────────────────────────
def test_screen_universe_unknown_preset_raises():
    with pytest.raises(ValueError):
        screen_universe("does-not-exist", ["AAA"])


def test_screen_ticker_uses_injected_builder():
    n = 220
    up = _ctx(list(np.linspace(10, 30, n)), [300_000] * n, {"returnOnEquity": 0.25})
    res = screen_ticker(BREAKOUT, "AAA", context_builder=lambda t: up)
    assert res.passed
    # and a downtrend fails via the same injected path
    down = _ctx(list(np.linspace(30, 10, n)), [300_000] * n)
    res2 = screen_ticker(BREAKOUT, "BBB", context_builder=lambda t: down)
    assert not res2.passed


def test_screen_ticker_handles_no_context():
    res = screen_ticker(BREAKOUT, "AAA", context_builder=lambda t: None)
    assert not res.passed and any("NO_DATA" in x for x in res.reasons)
