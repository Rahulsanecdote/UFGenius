"""Portfolio-level risk engine (universal-engine roadmap Phase 4).

An ADVISORY, default-off layer of portfolio-wide pre-trade checks (gross
leverage, single-name weight, portfolio heat, correlated-cluster exposure,
equity drawdown) that complements — never replaces — the per-trade RiskGuard.
"""

from src.risk.engine import (
    PortfolioRiskEngine,
    RiskDecision,
    candidate_from_plan,
    holdings_from_portfolio,
    portfolio_summary,
)

__all__ = [
    "PortfolioRiskEngine",
    "RiskDecision",
    "candidate_from_plan",
    "holdings_from_portfolio",
    "portfolio_summary",
]
