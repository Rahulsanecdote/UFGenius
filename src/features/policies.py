"""Signal weighting policies with optional regime-aware adjustments."""

from __future__ import annotations


def resolve_signal_weights(
    base_weights: dict[str, float],
    *,
    regime: str | None = None,
    asset_class: str = "equity",
    enable_regime_weighting: bool = False,
) -> dict[str, float]:
    """
    Resolve final signal weights.

    By default this returns normalized base weights to preserve current behavior.
    When enabled, adjustments are mild and bounded so no single factor dominates.
    """
    weights = dict(base_weights or {})
    if not weights:
        return {
            "technical": 0.35,
            "volume": 0.20,
            "sentiment": 0.20,
            "fundamental": 0.15,
            "macro": 0.10,
        }

    if enable_regime_weighting and asset_class == "equity" and regime:
        if regime in {"MILD_BEAR", "BEAR_RISK_OFF"}:
            weights["macro"] = weights.get("macro", 0.10) + 0.05
            weights["sentiment"] = max(0.0, weights.get("sentiment", 0.20) - 0.03)
            weights["technical"] = max(0.0, weights.get("technical", 0.35) - 0.02)
        elif regime in {"BULL_RISK_ON", "MILD_BULL"}:
            weights["technical"] = weights.get("technical", 0.35) + 0.03
            weights["macro"] = max(0.0, weights.get("macro", 0.10) - 0.02)
            weights["fundamental"] = max(0.0, weights.get("fundamental", 0.15) - 0.01)

    total = sum(max(0.0, float(v)) for v in weights.values())
    if total <= 0:
        return resolve_signal_weights({}, regime=regime, asset_class=asset_class, enable_regime_weighting=False)

    return {k: max(0.0, float(v)) / total for k, v in weights.items()}

