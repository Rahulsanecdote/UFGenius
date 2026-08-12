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

    **Consumers MUST branch on ``action``, not on ``approved`` alone.**
    ``approved`` means "the position may proceed" — but for ``action ==
    "scale_down"`` it is deliberately True *at the reduced ``suggested_shares``*,
    not at the submitted size. A consumer that keys off ``approved`` and ignores
    ``action``/``suggested_shares`` would place the full unscaled order. The
    money-path gate is stricter still: it proceeds only on ``action == "approve"``
    and treats scale-down as a skip (see `execute_trade_plan`).
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

    `get_portfolio_data` emits each holding as ``shares`` + ``current`` (the
    per-share price), NOT a precomputed ``market_value``; the position's value is
    derived as ``shares x current`` (falling back to an explicit
    ``market_value``/``value`` if a caller supplies one). Without stop data in
    the snapshot, per-position open risk is unknown, so ``risk`` is left as
    ``None`` when no ``open_risk`` is supplied — an explicit *unknown* marker,
    not a misleading 0.0. The gate treats unknown risk as 0 (fail-open, never a
    false veto); `portfolio_summary` reports heat as *unavailable* rather than
    "no heat". Any ``returns`` the caller attaches are passed through so the
    correlation-cluster check can run when return history is available.
    """
    holdings = []
    for h in portfolio.get("holdings") or []:
        explicit_value = _f(h.get("market_value") or h.get("value"))
        derived_value = _f(h.get("shares")) * _f(h.get("current"))
        raw_risk = h.get("open_risk")
        holdings.append(
            {
                "ticker": str(h.get("ticker", "")).upper(),
                "value": explicit_value or derived_value,
                "risk": _f(raw_risk) if raw_risk is not None else None,
                "returns": h.get("returns"),
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
        """Combined weight of the candidate's correlated cluster — the full
        connected component of names linked by pairwise correlation ≥ threshold.

        Correlation is transitive here: if the candidate correlates with B and B
        correlates with C (even when candidate↔C is below the threshold), all
        three share a cluster. Built with union-find over every name that has
        enough return history (candidate + holdings), so holding↔holding links
        are honoured, not just direct candidate neighbours.

        Returns ``(candidate_weight, [candidate])`` when the candidate has too
        little return history to correlate — no cluster inflation, so the check
        degrades to the single-name check and never false-vetoes.
        """
        cand_ticker = str(candidate.get("ticker", "")).upper() or "CANDIDATE"
        cand_returns = candidate.get("returns")
        cand_value = _f(candidate.get("value"))
        cand_weight = cand_value / equity if equity > 0 else 0.0

        if not _has_history(cand_returns, self.min_return_history):
            return cand_weight, [cand_ticker]

        # Node 0 is the candidate; the rest are holdings with usable history.
        nodes = [{"ticker": cand_ticker, "returns": cand_returns, "value": cand_value}]
        for h in holdings:
            if _has_history(h.get("returns"), self.min_return_history):
                nodes.append(
                    {
                        "ticker": str(h.get("ticker", "")).upper(),
                        "returns": h.get("returns"),
                        "value": _f(h.get("value")),
                    }
                )

        n = len(nodes)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]  # path compression
                x = parent[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                corr = average_correlation(nodes[i]["returns"], [nodes[j]["returns"]])
                if np.isfinite(corr) and corr >= self.correlation_threshold:
                    parent[find(i)] = find(j)  # union

        root = find(0)
        members = [k for k in range(n) if find(k) == root]
        cluster_value = sum(nodes[k]["value"] for k in members)
        cluster_tickers = [nodes[k]["ticker"] for k in members]

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
    can flag breaches. **Never raises** — on an empty/unreadable book (including a
    non-dict holding) it returns the zero-valued snapshot rather than propagating.

    Heat honesty: broker holdings carry no stop data, so per-position open risk is
    unknown. When *no* holding supplies risk, ``portfolio_heat`` and its breach
    are reported as ``None`` (heat **unmeasured**, not "zero heat"); heat is only
    a number when at least one holding has explicit risk.
    """
    try:
        eng = engine or PortfolioRiskEngine()
        holdings = list(holdings or [])
        equity = _f(equity)

        existing_value = sum(_f(h.get("value")) for h in holdings)
        risk_known = any(h.get("risk") is not None for h in holdings)
        existing_risk = sum(_f(h.get("risk")) for h in holdings)
        weights = []
        if equity > 0:
            for h in holdings:
                w = _f(h.get("value")) / equity
                weights.append({"ticker": str(h.get("ticker", "")).upper(), "weight": round(w, 4)})
        weights.sort(key=lambda x: x["weight"], reverse=True)

        gross_leverage = existing_value / equity if equity > 0 else 0.0
        max_single = weights[0]["weight"] if weights else 0.0

        if risk_known and equity > 0:
            heat = existing_risk / equity
            portfolio_heat: float | None = round(heat, 4)
            heat_breach: bool | None = heat > eng.max_heat
        else:
            portfolio_heat = None  # unmeasured (no stop data on broker holdings)
            heat_breach = None

        return {
            "equity": round(equity, 2),
            "position_count": len(holdings),
            "gross_leverage": round(gross_leverage, 4),
            "portfolio_heat": portfolio_heat,
            "drawdown": round(_drawdown(equity, peak_equity), 4),
            "max_single_weight": round(max_single, 4),
            "holdings": weights,
            "breaches": {
                "gross_leverage": gross_leverage > eng.max_gross_leverage,
                "portfolio_heat": heat_breach,
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
    except Exception:  # advisory read-only snapshot must never raise
        log.exception("portfolio_summary failed; returning empty snapshot")
        return {
            "equity": 0.0,
            "position_count": 0,
            "gross_leverage": 0.0,
            "portfolio_heat": None,
            "drawdown": 0.0,
            "max_single_weight": 0.0,
            "holdings": [],
            "breaches": {"gross_leverage": False, "portfolio_heat": None, "single_weight": False},
            "limits": {},
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
