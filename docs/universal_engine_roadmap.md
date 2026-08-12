# Universal Financial Engine Roadmap

This repository already has a solid stock-scanner core. The next evolution is a
multi-asset analysis and decision engine with strict separation:

1. `data ingestion`
2. `feature/signal generation`
3. `portfolio + risk decisions`
4. `execution simulation/live routing`
5. `evaluation + monitoring`

## Design Principles

- Single canonical data contracts across all asset classes.
- Strategy logic never calls providers directly (provider adapters only).
- Risk engine has veto power on all orders.
- Research, paper, and live environments share the same core path.
- Every model/signal output is attributable and backtest-reproducible.

## Phase 1 (Current Sprint): Data Foundation Hardening

### Objectives

- Make external data access resilient, testable, and deterministic.
- Eliminate ad-hoc network call behavior and cache inconsistencies.

### Implemented in this sprint

- `src/data/fetcher.py`
  - Added timeout-bounded yfinance wrappers.
  - Unified bounded retries via `src.utils.http.retry_call`.
  - Added cache control API (`clear_data_caches`) for deterministic tests.
  - Improved payload validation (required OHLCV columns, uppercase symbols).
- `.gitignore`
  - Added `.venv/` to avoid accidental environment commits.

## Phase 2: Canonical Multi-Asset Contracts

### Target files

- `src/core/models.py` (new)
- `src/core/contracts.py` (new)
- `src/data/providers/*.py` (new)

### Scope

- Introduce typed entities: `Instrument`, `Bar`, `Quote`, `Fundamentals`,
  `MacroPoint`, `SignalPacket`, `PortfolioSnapshot`, `RiskDecision`.
- Add provider interfaces for historical bars, fundamentals, options chains,
  crypto market data, forex, and macro series.
- Add fallback provider chain (primary/secondary).

### Implemented

- `src/core/models.py`
  - Added canonical entities (`Instrument`, `Fundamentals`, `TickerSnapshot`).
- `src/core/contracts.py`
  - Added typed provider contracts for OHLCV/info/fundamentals/snapshots.
- `src/data/providers/*`
  - Added default yfinance snapshot adapter and provider registry.
- `src/signals/context.py`, `src/signals/generator.py`
  - Added provider-injected signal context path.

## Phase 3: Signal and Feature Store

### Target files

- `src/features/*.py` (new)
- `src/signals/generator.py` (refactor to typed inputs)
- `src/signals/context.py` (expand to canonical snapshot)

### Scope

- Centralized feature registry (momentum, carry, value, volatility, sentiment).
- Reusable feature cache with TTL and data-version keys.
- Regime-aware weighting and per-asset scoring policies.

### Implemented

- `src/features/signal_features.py`
  - Added centralized feature registry and cache-backed technical feature bundle.
- `src/features/store.py`
  - Added TTL + version keyed in-memory feature store with bounded size.
- `src/features/policies.py`
  - Added optional regime-aware signal weight policy resolver.
- `src/signals/generator.py`
  - Migrated technical computation to registry/store path and added feature metadata.
- `tests/test_phase3_features.py`
  - Added cache, policy, and generator integration coverage.

## Phase 4: Portfolio + Risk Engine

### Target files

- `src/portfolio/optimizer.py` (new)
- `src/risk/engine.py` (new)
- `src/backtest/engine.py` (integrate risk decisions)

### Scope

- Position sizing by volatility and correlation contribution.
- Hard pre-trade checks: leverage, concentration, drawdown guardrails, max loss.
- Portfolio optimization mode (baseline: constrained mean-variance / HRP).

### Implemented

Delivered as an **advisory, default-off** layer that *complements* — never
replaces — the per-trade `RiskGuard`. With `portfolio.enabled=false` it is a
no-op; it is surfaced for review and only ever *tightens* (veto / scale-down),
never loosens a rule or enlarges a position.

- `src/portfolio/sizing.py`
  - Pure numpy/pandas (no scipy/sklearn) sizing primitives: `periodic_returns`,
    `annualized_volatility`, `inverse_variance_weights` (naive **risk-parity**
    baseline — the return-free, stable alternative to unstable mean-variance),
    `volatility_target_weight`, `average_correlation`, `correlation_scaled_shares`,
    and a `suggest_shares` orchestrator layering risk-budget → capital-cap →
    volatility-target → correlation-penalty. Every function is total (returns
    `nan`/empty/0 on bad input, never raises).
- `src/risk/engine.py`
  - `PortfolioRiskEngine.evaluate` → a `RiskDecision` (approve / scale_down /
    veto) from portfolio-wide checks `RiskGuard` cannot see: **gross leverage**,
    **single-name weight**, **portfolio heat** (Σ open risk / equity),
    **correlated-cluster exposure** (the full *connected component* — union-find
    over candidate + holdings, so transitive A–B–C correlation clusters count,
    not just direct candidate neighbours), and an **equity-drawdown guardrail**.
    A cluster over its cap *scales down* (suggests a share count that fits)
    rather than vetoing outright. **Firewalled from the money path** (no
    executor/broker import) and fail-open: any internal error returns an
    *approve* tagged with the error, so a bug here can never silently block
    trading.
  - `holdings_from_portfolio()` derives each position's value from the broker's
    real fields (`shares × current`), since `get_portfolio_data` emits shares +
    per-share price, not a precomputed market value.
  - `portfolio_summary()` — read-only book snapshot (leverage / heat / per-name
    weights vs limits) for the dashboard.
- `src/risk/peak_equity.py` — `PeakEquityTracker`, a persisted (atomic +
  `flock`) equity **high-water mark** so the drawdown halt has a peak to measure
  against; fails open (no peak known → drawdown 0). Fed by both the gate and the
  advisory endpoint.
- **Opt-in money-path gate** (`config.PORTFOLIO_GATE_ENTRIES`, default off): when
  enabled, `execute_trade_plan` consults the engine **after** `RiskGuard` already
  approved — veto-only (proceeds only on an explicit approve; the full
  scale-down suggestion is surfaced advisory-side for a human/upstream), and
  fail-open so it never breaks execution. **Enforced at the exec call site:**
  gross leverage, single-name weight, portfolio heat (real book values), and the
  drawdown halt (via the high-water mark). **Not enforced there:** correlated-
  cluster exposure — the executor doesn't fetch aligned intraday return
  histories, so that check is advisory-surface only and no-ops in the live gate
  (never a false veto).
- Surfaced via `GET /api/portfolio-risk` (advisory; `available:false` when
  disabled/unreadable). Config-driven (`config.yaml` `portfolio:` +
  `PORTFOLIO_*` env). 63 offline tests (`tests/test_portfolio_sizing.py`,
  `tests/test_risk_engine.py`, `tests/test_peak_equity.py`, + dashboard-API and
  executor-gate cases).

## Phase 5: Strategy Evaluation and Automation

### Target files

- `src/eval/walkforward.py` (new)
- `src/eval/attribution.py` (new)
- `tests/test_eval_*.py` (new)

### Scope

- Walk-forward evaluation and out-of-sample gating.
- Transaction cost/slippage models per asset class.
- Champion/challenger workflow for promotion to paper/live.

### Delivered (by mapping)

This phase's scope was delivered under the **`UPGRADE_PLAN.md` P0/P2 track**, so
the work lives in existing packages rather than the proposed `src/eval/*` files.
The `src/eval/` module was never created — the functionality below supersedes it.

| Phase 5 scope item | Delivered by |
|---|---|
| Walk-forward evaluation + OOS gating | `src/backtest/validation.py` (`walk_forward`, `validate_strategy`, `bootstrap_trade_metrics`) + `python bot.py --mode validate` — **P0.1**. Parameter selection scored only on validation folds with an overfitting haircut: `src/backtest/optimize.py` (`parameter_search`, `expected_max_sharpe`) + `--mode optimize` — **P0.2**. |
| Transaction cost / slippage models | Backtest cost model (`commission_pct`/`slippage_pct`, next-bar-open fills) + **measured** slippage fed back from real fills: `src/alpaca/execution_quality.py` (`use_measured_slippage`) — **P2.1**. (Per-asset-class models remain single-asset today — equities only — since the universe is equities.) |
| Attribution | `src/observability/attribution.py` — per-signal-label realized outcome over the P0.4 trade-outcome ledger — **P2.3**. |
| Promotion to paper/live | `src/alpaca/scorecard.py` — the paper scorecard gate that requires realized paper metrics to match the validated backtest (same `bootstrap_trade_metrics` estimator) before the live flag — **P0.4**. |

**Genuine remaining gap:** a *formal champion/challenger* workflow — running a
candidate strategy against the incumbent and auto-promoting on a measured edge —
does **not** exist. The pieces are in place (validation harness + paper
scorecard as the promotion gate); what's missing is the orchestration that pits
two parameter sets/strategies against each other and records a promotion
decision. Left as the one open Phase 5 item.

## Phase 6: Operational Intelligence Layer

### Target files

- `dashboard.py` (model/strategy introspection views)
- `src/alerts/*` (risk/system alert routing)
- `src/ops/metrics.py` (new)

### Scope

- Structured metrics: latency, fill quality, risk breaches, PnL attribution.
- LLM research copilot for explanation and anomaly triage (read-only to execution).
- Incident-level audit trail for all strategy and risk decisions.

### Delivered (by mapping)

Delivered under the **`UPGRADE_PLAN.md` P0.3/P2.3/P3.1 track**; the proposed
`src/ops/metrics.py` was never created — `src/observability/` fills that role.

| Phase 6 scope item | Delivered by |
|---|---|
| Structured metrics — latency | `src/observability/metrics.py` (`MetricsLedger`: per-scan latency avg/p95, signal counts, data-gap flag), `GET /api/metrics` — **P2.3**. |
| Structured metrics — fill quality | `src/alpaca/execution_quality.py` (realized slippage / implementation shortfall), `GET /api/execution-quality` — **P2.1**. |
| Structured metrics — risk breaches | `src/alpaca/circuit_breaker.py` (halt / broker-error / data-staleness state), `GET /api/breaker-state` — **P0.3**; alerting on trips via `src/observability/alerting.py` — **P2.3**. |
| Structured metrics — PnL attribution | `src/observability/attribution.py`, `GET /api/attribution` — **P2.3**. |
| LLM research copilot (read-only to execution) | `src/explain/narrative.py` — optional bull/bear narrative over the **verified snapshot**, structurally firewalled from the money path, prompt-injection-sandboxed, cost-capped, `GET /api/explain` — **P3.1**. |
| Alert routing | `src/observability/alerting.py` + `src/alerts/*` (`send_text_alert`/telegram/email) — **P2.3**. |

**Partial gap — unified audit trail:** decision/outcome history *is* persisted,
but across several purpose-built ledgers (P0.4 trade-outcome, P2.1
execution-quality, P2.3 scan-metrics, P0.3 circuit-breaker trail) rather than one
consolidated incident stream. A single append-only "why did the bot do X at
time T" audit log that stitches these together does not yet exist; the raw
material for it does. Left as the open Phase 6 item.

## Definition of Done (Per Phase)

- All new modules have unit tests and integration tests.
- Backtests reconcile accounting and risk constraints deterministically.
- API surfaces return sanitized errors only.
- Docs and `.env.example` are aligned with runtime behavior.
