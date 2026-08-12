"""Portfolio construction + sizing (universal-engine roadmap Phase 4).

Advisory, dependency-light (numpy/pandas only) helpers for volatility- and
correlation-aware position sizing. Pure math — no provider, broker, or executor
imports — so it is safe to reuse from the risk engine, dashboard, or backtest.
"""

from src.portfolio.sizing import (
    annualized_volatility,
    average_correlation,
    correlation_scaled_shares,
    inverse_variance_weights,
    periodic_returns,
    suggest_shares,
    volatility_target_weight,
)

__all__ = [
    "annualized_volatility",
    "average_correlation",
    "correlation_scaled_shares",
    "inverse_variance_weights",
    "periodic_returns",
    "suggest_shares",
    "volatility_target_weight",
]
