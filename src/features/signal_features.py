"""Centralized feature registry for signal generation with cache-backed execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from src.features.store import FeatureStore
from src.technical.momentum import calculate_momentum_indicators, score_momentum
from src.technical.trend import calculate_trend_indicators, score_trend
from src.technical.volatility import calculate_volatility_indicators
from src.technical.volume import calculate_volume_indicators, score_volume
from src.utils import config

FeatureBuilder = Callable[["FeatureContext", Callable[[str], Any]], Any]


@dataclass
class FeatureContext:
    ticker: str
    price_df: pd.DataFrame
    current_price: float
    market_regime: str | None = None


class FeatureRegistry:
    def __init__(self):
        self._builders: dict[str, FeatureBuilder] = {}

    def register(self, name: str, builder: FeatureBuilder) -> None:
        self._builders[name] = builder

    def compute_many(self, names: list[str], ctx: FeatureContext) -> dict[str, Any]:
        memo: dict[str, Any] = {}

        def get(name: str) -> Any:
            if name in memo:
                return memo[name]
            builder = self._builders.get(name)
            if builder is None:
                raise KeyError(f"Unknown feature: {name}")
            memo[name] = builder(ctx, get)
            return memo[name]

        return {name: get(name) for name in names}


_DEFAULT_FEATURE_STORE: FeatureStore | None = None
_DEFAULT_FEATURE_REGISTRY: FeatureRegistry | None = None


def get_default_feature_store() -> FeatureStore:
    global _DEFAULT_FEATURE_STORE
    if _DEFAULT_FEATURE_STORE is None:
        _DEFAULT_FEATURE_STORE = FeatureStore(max_entries=config.FEATURE_CACHE_MAX_ENTRIES)
    return _DEFAULT_FEATURE_STORE


def get_default_feature_registry() -> FeatureRegistry:
    global _DEFAULT_FEATURE_REGISTRY
    if _DEFAULT_FEATURE_REGISTRY is None:
        _DEFAULT_FEATURE_REGISTRY = _build_default_registry()
    return _DEFAULT_FEATURE_REGISTRY


def clear_signal_feature_cache() -> None:
    get_default_feature_store().clear()


def compute_signal_features(
    *,
    ticker: str,
    price_df: pd.DataFrame,
    current_price: float,
    market_regime: str | None = None,
    store: FeatureStore | None = None,
    registry: FeatureRegistry | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    Compute feature bundle for signal generation.

    Returns:
      - dict of computed features
      - cache_hit flag
    """
    if price_df is None or price_df.empty:
        return {}, False

    if store is None:
        store = get_default_feature_store()
    if registry is None:
        registry = get_default_feature_registry()

    cache_key = _build_feature_cache_key(
        ticker=ticker,
        price_df=price_df,
        market_regime=market_regime,
    )
    version = config.FEATURE_CACHE_VERSION
    cached = store.get(cache_key, version=version)
    if cached is not None:
        return cached, True

    ctx = FeatureContext(
        ticker=ticker.upper(),
        price_df=price_df,
        current_price=current_price,
        market_regime=market_regime,
    )
    bundle = registry.compute_many(
        [
            "trend_indicators",
            "momentum_indicators",
            "volatility_indicators",
            "volume_indicators",
            "trend_score",
            "momentum_score",
            "volume_score",
            "technical_combined",
        ],
        ctx,
    )
    bundle["feature_cache_key"] = cache_key
    bundle["feature_version"] = version
    store.set(
        cache_key,
        bundle,
        ttl_sec=config.FEATURE_CACHE_TTL_SEC,
        version=version,
    )
    return bundle, False


def _build_feature_cache_key(
    *,
    ticker: str,
    price_df: pd.DataFrame,
    market_regime: str | None,
) -> str:
    symbol = ticker.upper()
    rows = len(price_df)
    idx = price_df.index[-1] if rows else "none"
    close = float(price_df["Close"].iloc[-1]) if rows else 0.0
    volume = float(price_df["Volume"].iloc[-1]) if rows and "Volume" in price_df else 0.0
    regime = market_regime or "UNKNOWN"
    return f"{symbol}:{rows}:{idx}:{close:.6f}:{volume:.2f}:{regime}"


def _build_default_registry() -> FeatureRegistry:
    registry = FeatureRegistry()

    registry.register(
        "trend_indicators",
        lambda ctx, _get: calculate_trend_indicators(ctx.price_df),
    )
    registry.register(
        "momentum_indicators",
        lambda ctx, _get: calculate_momentum_indicators(ctx.price_df),
    )
    registry.register(
        "volatility_indicators",
        lambda ctx, _get: calculate_volatility_indicators(ctx.price_df),
    )
    registry.register(
        "volume_indicators",
        lambda ctx, _get: calculate_volume_indicators(ctx.price_df),
    )
    registry.register(
        "trend_score",
        lambda ctx, get: score_trend(get("trend_indicators"), ctx.current_price),
    )
    registry.register(
        "momentum_score",
        lambda ctx, get: score_momentum(get("momentum_indicators")),
    )
    registry.register(
        "volume_score",
        lambda ctx, get: score_volume(get("volume_indicators")),
    )
    registry.register(
        "technical_combined",
        lambda _ctx, get: get("trend_score")["score"] * 0.65 + get("momentum_score")["score"] * 0.35,
    )
    return registry

