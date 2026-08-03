"""Phase 3 tests for feature registry/store and generator integration."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.core.models import Instrument
from src.features.policies import resolve_signal_weights
from src.features.signal_features import clear_signal_feature_cache, compute_signal_features
from src.features.store import FeatureStore
from src.signals import generator
from src.signals.context import SignalContext
from src.utils import config


def _sample_price_df(rows: int = 260) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=rows, freq="B")
    close = np.linspace(100, 130, rows)
    return pd.DataFrame(
        {
            "Open": close * 0.99,
            "High": close * 1.01,
            "Low": close * 0.98,
            "Close": close,
            "Volume": np.full(rows, 1_000_000.0),
        },
        index=idx,
    )


def _sample_fundamentals() -> dict:
    return {
        "ticker": "TEST",
        "market_cap": 10_000_000_000,
        "revenue": 10_000_000_000,
        "gross_profit": 5_000_000_000,
        "ebit": 2_000_000_000,
        "ebitda": 2_500_000_000,
        "net_income": 1_500_000_000,
        "total_assets": 8_000_000_000,
        "total_liabilities": 3_000_000_000,
        "current_assets": 2_000_000_000,
        "current_liabilities": 1_000_000_000,
        "retained_earnings": 1_200_000_000,
        "total_debt": 500_000_000,
        "operating_cash_flow": 1_000_000_000,
        "enterprise_value": 11_000_000_000,
        "free_cash_flow": 800_000_000,
        "revenue_growth_yoy": 0.1,
        "peg_ratio": 1.1,
    }


def test_feature_store_ttl_and_eviction(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("src.features.store.time.time", lambda: now["t"])

    store = FeatureStore(max_entries=2)
    store.set("a", {"v": 1}, ttl_sec=5, version="v1")
    store.set("b", {"v": 2}, ttl_sec=5, version="v1")
    store.set("c", {"v": 3}, ttl_sec=5, version="v1")

    assert store.get("a", version="v1") is None  # evicted oldest
    assert store.get("b", version="v1") is not None

    now["t"] = 1010.0
    assert store.get("b", version="v1") is None  # expired by TTL


def test_compute_signal_features_cache_hit():
    clear_signal_feature_cache()
    df = _sample_price_df()
    one, hit_one = compute_signal_features(
        ticker="TEST",
        price_df=df,
        current_price=float(df["Close"].iloc[-1]),
        market_regime="NEUTRAL_CHOPPY",
    )
    two, hit_two = compute_signal_features(
        ticker="TEST",
        price_df=df,
        current_price=float(df["Close"].iloc[-1]),
        market_regime="NEUTRAL_CHOPPY",
    )
    assert hit_one is False
    assert hit_two is True
    assert one["technical_combined"] == two["technical_combined"]


def test_resolve_signal_weights_regime_adjustment_toggle():
    base = {"technical": 0.35, "volume": 0.20, "sentiment": 0.20, "fundamental": 0.15, "macro": 0.10}
    unchanged = resolve_signal_weights(base, regime="BEAR_RISK_OFF", enable_regime_weighting=False)
    adjusted = resolve_signal_weights(base, regime="BEAR_RISK_OFF", enable_regime_weighting=True)

    assert abs(sum(unchanged.values()) - 1.0) < 1e-9
    assert abs(sum(adjusted.values()) - 1.0) < 1e-9
    assert adjusted["macro"] > unchanged["macro"]


def test_generate_signal_feature_metadata_and_cache(monkeypatch):
    clear_signal_feature_cache()
    df = _sample_price_df()
    ctx = SignalContext(
        ticker="TEST",
        price_df=df,
        ticker_info={"longName": "Test Corp"},
        fundamentals_raw=_sample_fundamentals(),
        instrument=Instrument(symbol="TEST"),
        provider="unit",
    )

    monkeypatch.setattr(generator, "analyze_news_sentiment", lambda *_a, **_k: {"sentiment_score_0_100": 55, "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "analyze_social_sentiment", lambda *_a, **_k: {"sentiment_score_0_100": 50, "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "analyze_insider_activity", lambda *_a, **_k: {"insider_score": 50, "flags": [], "signal": "NEUTRAL"})
    monkeypatch.setattr(generator, "detect_market_regime", lambda: {"regime": "NEUTRAL_CHOPPY", "regime_score": 0, "strategy": {"position_size_multiplier": 0.5}})

    first = generator.generate_signal("TEST", context=ctx)
    second = generator.generate_signal("TEST", context=ctx)

    assert first["_feature_cache_hit"] is False
    assert second["_feature_cache_hit"] is True
    assert first["_feature_cache_key"] == second["_feature_cache_key"]
    assert first["_feature_version"] == config.FEATURE_CACHE_VERSION

