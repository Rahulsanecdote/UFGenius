"""Portfolio-level risk engine (roadmap Phase 4).

`PortfolioRiskEngine.evaluate` scores a candidate entry against the *whole book*
— checks the per-trade `RiskGuard` cannot see because it reasons about one order
at a time:

  * **gross leverage** — Σ position value (incl. candidate) / equity
  * **single-name weight** — candidate value / equity (portfolio-level cap)
  * **portfolio heat** — Σ open risk (incl. candidate) / equity
  * **correlated-cluster exposure** — combined weight of the candidate's cluster
    of return-correlated names (connected via pairwise correlation ≥ threshold)
  * **equity drawdown guardrail** — halt new entries after a peak-to-trough drop

It is **advisory and firewalled from the money path**: no executor/broker import,
and every public method is total — on any internal error it returns an *approve*
decision tagged with the error, so a bug here can never silently block trading.
It only ever *tightens* (veto / scale-down); it never enlarges a position or
loosens a RiskGuard rule. Wiring it into the live path is a separate opt-in
(`config.PORTFOLIO_GATE_ENTRIES`, default off) that runs only after RiskGuard
already approved.

Thresholds default from `config.py`; every one is constructor-overridable so the
engine is fully unit-testable without touching global config.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import numpy as np

from src.portfolio.sizing import average_correlation
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

APPROVE = "approve"
SCALE_DOWN = "scale_down"
VETO = "veto"


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of a portfolio-level risk evaluation.

    ``advisory`` is always True: even when ``action == "veto"`` the engine only
    *recommends* — enforcement is the caller's choice (default: surface only).
    """

    approved: bool
    action: str  # APPROVE | SCALE_DOWN | VETO
    suggested_shares: int | None
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    advisory: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def candidate_from_plan(plan: dict, returns: Sequence | None = None) -> dict:
    """Normalize a trade-plan dict into the engine's candidate shape.

    Reads the standard `generate_trade_plan` layout (``position`` /
    ``entry``) and fails soft — missing numbers become 0.0.
    """
    position = plan.get("position") or {}
    entry = plan.get("entry") or {}
    price = entry.get("price") or entry.get("reference_price") or 0.0
    return {
        "ticker": str(plan.get("ticker", "")).upper(),
        "value": _f(position.get("position_value")),
        "risk": _f(position.get("risk_dollars")),
        "price": _f(price),
        "shares": int(_f(position.get("shares"))),
        "returns": returns,
    }


def holdings_from_portfolio(portfolio: dict) -> list[dict]:
    """Map an Alpaca portfolio dict's holdings into the engine's holding shape.

    Return history is not attached here (the live path has none readily), so the
    correlation-cluster check is simply skipped downstream — a missing estimate
    never produces a false veto.
    """
    holdings = []
    for h in portfolio.get("holdings") or []:
        holdings.append(
            {
                "ticker": str(h.get("ticker", "")).upper(),
                "value": _f(h.get("market_value") or h.get("value")),
                "risk": _f(h.get("open_risk")),
                "returns": None,
            }
        )
    return holdings


def _f(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if np.isfinite(f) else 0.0


class PortfolioRiskEngine:
    def __init__(
        self,
        *,
        max_gross_leverage: float | None = None,
        max_single_weight_pct: float | None = None,
        max_portfolio_heat_pct: float | None = None,
        correlation_threshold: float | None = None,
        max_cluster_weight_pct: float | None = None,
        max_drawdown_halt_pct: float | None = None,
        min_return_history: int | None = None,
    ) -> None:
        self.max_gross_leverage = _cfg(max_gross_leverage, config.PORTFOLIO_MAX_GROSS_LEVERAGE)
        self.max_single_weight = _cfg(max_single_weight_pct, config.PORTFOLIO_MAX_SINGLE_WEIGHT_PCT) / 100.0
        self.max_heat = _cfg(max_portfolio_heat_pct, config.PORTFOLIO_MAX_HEAT_PCT) / 100.0
        self.correlation_threshold = _cfg(correlation_threshold, config.PORTFOLIO_CORRELATION_THRESHOLD)
        self.max_cluster_weight = _cfg(max_cluster_weight_pct, config.PORTFOLIO_MAX_CLUSTER_WEIGHT_PCT) / 100.0
        self.max_drawdown_halt = _cfg(max_drawdown_halt_pct, config.PORTFOLIO_MAX_DRAWDOWN_HALT_PCT) / 100.0
        self.min_return_history = int(_cfg(min_return_history, config.PORTFOLIO_MIN_RETURN_HISTORY))

    def evaluate(
        self,
        candidate: dict,
        holdings: Sequence[dict],
        equity: float,
        peak_equity: float | None = None,
    ) -> RiskDecision:
        """Advisory portfolio-level ruling on adding ``candidate`` to the book.

        Never raises. Hard breaches (leverage / single-name / heat / drawdown)
        → VETO; a correlated cluster over its cap → SCALE_DOWN with a suggested
        share count that brings the cluster back within limit (or VETO if the
        cluster is already full without the candidate). Otherwise APPROVE.
        """
        try:
            return self._evaluate(candidate, holdings, equity, peak_equity)
        except Exception:  # advisory layer must never break the caller
            log.exception("PortfolioRiskEngine.evaluate failed; failing open (approve)")
            return RiskDecision(
                approved=True,
                action=APPROVE,
                suggested_shares=int(_f((candidate or {}).get("shares"))) or None,
                reasons=["engine_error: evaluation failed, failing open (advisory)"],
                metrics={},
            )

    def _evaluate(
        self,
        candidate: dict,
        holdings: Sequence[dict],
        equity: float,
        peak_equity: float | None,
    ) -> RiskDecision:
        candidate = candidate or {}
        holdings = list(holdings or [])
        cand_value = _f(candidate.get("value"))
        cand_risk = _f(candidate.get("risk"))
        cand_price = _f(candidate.get("price"))
        cand_shares = int(_f(candidate.get("shares")))

        if equity <= 0:
            return RiskDecision(
                approved=True,
                action=APPROVE,
                suggested_shares=cand_shares or None,
                reasons=["equity unavailable; portfolio checks skipped (advisory)"],
                metrics={"equity": equity},
            )

        existing_value = sum(_f(h.get("value")) for h in holdings)
        existing_risk = sum(_f(h.get("risk")) for h in holdings)

        gross_leverage = (existing_value + cand_value) / equity
        single_weight = cand_value / equity
        portfolio_heat = (existing_risk + cand_risk) / equity
        drawdown = _drawdown(equity, peak_equity)
        cluster_weight, cluster_tickers = self._cluster_weight(candidate, holdings, equity)

        metrics = {
            "equity": round(equity, 2),
            "existing_value": round(existing_value, 2),
            "candidate_value": round(cand_value, 2),
            "gross_leverage": round(gross_leverage, 4),
            "single_weight": round(single_weight, 4),
            "portfolio_heat": round(portfolio_heat, 4),
            "drawdown": round(drawdown, 4),
            "cluster_weight": round(cluster_weight, 4),
            "cluster_tickers": cluster_tickers,
            "limits": {
                "max_gross_leverage": self.max_gross_leverage,
                "max_single_weight": self.max_single_weight,
                "max_portfolio_heat": self.max_heat,
                "max_cluster_weight": self.max_cluster_weight,
                "max_drawdown_halt": self.max_drawdown_halt,
                "correlation_threshold": self.correlation_threshold,
            },
        }

        veto_reasons: list[str] = []
        if self.max_drawdown_halt > 0 and drawdown >= self.max_drawdown_halt:
            veto_reasons.append(
                f"equity drawdown {drawdown:.1%} ≥ halt {self.max_drawdown_halt:.1%}"
            )
        if gross_leverage > self.max_gross_leverage:
            veto_reasons.append(
                f"gross leverage {gross_leverage:.2f}x > cap {self.max_gross_leverage:.2f}x"
            )
        if single_weight > self.max_single_weight:
            veto_reasons.append(
                f"single-name weight {single_weight:.1%} > cap {self.max_single_weight:.1%}"
            )
        if portfolio_heat > self.max_heat:
            veto_reasons.append(
                f"portfolio heat {portfolio_heat:.1%} > cap {self.max_heat:.1%}"
            )

        if veto_reasons:
            return RiskDecision(
                approved=False,
                action=VETO,
                suggested_shares=0,
                reasons=veto_reasons,
                metrics=metrics,
            )

        # Correlated-cluster exposure — a soft cap that scales rather than vetoes
        # (unless the cluster is already full without the candidate).
        if self.max_cluster_weight > 0 and cluster_weight > self.max_cluster_weight:
            existing_cluster_value = max(0.0, cluster_weight * equity - cand_value)
            room = self.max_cluster_weight * equity - existing_cluster_value
            if room <= 0 or cand_price <= 0:
                return RiskDecision(
                    approved=False,
                    action=VETO,
                    suggested_shares=0,
                    reasons=[
                        f"correlated cluster {cluster_tickers} weight "
                        f"{cluster_weight:.1%} > cap {self.max_cluster_weight:.1%}; "
                        "no room for the candidate"
                    ],
                    metrics=metrics,
                )
            scaled_shares = int(room / cand_price)
            if scaled_shares < cand_shares:
                return RiskDecision(
                    approved=True,
                    action=SCALE_DOWN,
                    suggested_shares=max(0, scaled_shares),
                    reasons=[
                        f"correlated cluster {cluster_tickers} weight "
                        f"{cluster_weight:.1%} > cap {self.max_cluster_weight:.1%}; "
                        f"scale {cand_shares} → {max(0, scaled_shares)} shares"
                    ],
                    metrics=metrics,
                )

        return RiskDecision(
            approved=True,
            action=APPROVE,
            suggested_shares=cand_shares or None,
            reasons=["within portfolio risk limits"],
            metrics=metrics,
        )

    def _cluster_weight(
        self,
        candidate: dict,
        holdings: Sequence[dict],
        equity: float,
    ) -> tuple[float, list[str]]:
        """Combined weight of the candidate's correlated cluster (candidate + all
        holdings connected to it via pairwise correlation ≥ threshold).

        Returns ``(candidate_weight, [candidate])`` when the candidate has too
        little return history to correlate — i.e. no cluster inflation, so the
        cluster check degrades to the single-name check and never false-vetoes.
        """
        cand_ticker = str(candidate.get("ticker", "")).upper() or "CANDIDATE"
        cand_returns = candidate.get("returns")
        cand_value = _f(candidate.get("value"))
        cand_weight = cand_value / equity if equity > 0 else 0.0

        if not _has_history(cand_returns, self.min_return_history):
            return cand_weight, [cand_ticker]

        # Build the candidate's connected component greedily: any holding whose
        # returns correlate with the candidate at/above the threshold joins.
        cluster_value = cand_value
        cluster_tickers = [cand_ticker]
        for h in holdings:
            h_returns = h.get("returns")
            if not _has_history(h_returns, self.min_return_history):
                continue
            corr = average_correlation(cand_returns, [h_returns])
            if np.isfinite(corr) and corr >= self.correlation_threshold:
                cluster_value += _f(h.get("value"))
                cluster_tickers.append(str(h.get("ticker", "")).upper())

        return (cluster_value / equity if equity > 0 else 0.0), cluster_tickers


def portfolio_summary(
    holdings: Sequence[dict],
    equity: float,
    peak_equity: float | None = None,
    engine: "PortfolioRiskEngine | None" = None,
) -> dict:
    """Read-only, book-level risk snapshot (no candidate) for the dashboard.

    Pure analytics: gross leverage, portfolio heat, drawdown, and per-name
    weights of the *current* holdings, alongside the configured limits so the UI
    can flag breaches. Never raises — returns zeros on an empty/unreadable book.
    """
    eng = engine or PortfolioRiskEngine()
    holdings = list(holdings or [])
    equity = _f(equity)

    existing_value = sum(_f(h.get("value")) for h in holdings)
    existing_risk = sum(_f(h.get("risk")) for h in holdings)
    weights = []
    if equity > 0:
        for h in holdings:
            w = _f(h.get("value")) / equity
            weights.append({"ticker": str(h.get("ticker", "")).upper(), "weight": round(w, 4)})
    weights.sort(key=lambda x: x["weight"], reverse=True)

    gross_leverage = existing_value / equity if equity > 0 else 0.0
    portfolio_heat = existing_risk / equity if equity > 0 else 0.0
    max_single = weights[0]["weight"] if weights else 0.0

    return {
        "equity": round(equity, 2),
        "position_count": len(holdings),
        "gross_leverage": round(gross_leverage, 4),
        "portfolio_heat": round(portfolio_heat, 4),
        "drawdown": round(_drawdown(equity, peak_equity), 4),
        "max_single_weight": round(max_single, 4),
        "holdings": weights,
        "breaches": {
            "gross_leverage": gross_leverage > eng.max_gross_leverage,
            "portfolio_heat": portfolio_heat > eng.max_heat,
            "single_weight": max_single > eng.max_single_weight,
        },
        "limits": {
            "max_gross_leverage": eng.max_gross_leverage,
            "max_single_weight": eng.max_single_weight,
            "max_portfolio_heat": eng.max_heat,
            "max_cluster_weight": eng.max_cluster_weight,
            "max_drawdown_halt": eng.max_drawdown_halt,
            "correlation_threshold": eng.correlation_threshold,
        },
    }


def _has_history(returns, minimum: int) -> bool:
    if returns is None:
        return False
    try:
        arr = np.asarray(returns, dtype=float).ravel()
    except (TypeError, ValueError):
        return False
    arr = arr[np.isfinite(arr)]
    return arr.size >= max(2, minimum)


def _drawdown(equity: float, peak_equity: float | None) -> float:
    if peak_equity is None or peak_equity <= 0 or equity >= peak_equity:
        return 0.0
    return (peak_equity - equity) / peak_equity


def _cfg(override, default):
    return default if override is None else override
