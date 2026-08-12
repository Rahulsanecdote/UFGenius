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
    **correlated-cluster exposure** (connected component of names with pairwise
    return-correlation ≥ threshold), and an **equity-drawdown guardrail**. A
    cluster over its cap *scales down* (suggests a share count that fits) rather
    than vetoing outright. **Firewalled from the money path** (no executor/broker
    import) and fail-open: any internal error returns an *approve* tagged with
    the error, so a bug here can never silently block trading.
  - `portfolio_summary()` — read-only book snapshot (leverage / heat / per-name
    weights vs limits) for the dashboard.
- **Opt-in money-path gate** (`config.PORTFOLIO_GATE_ENTRIES`, default off): when
  enabled, `execute_trade_plan` consults the engine **after** `RiskGuard` already
  approved — veto-only (proceeds only on an explicit approve; the full
  scale-down suggestion is surfaced advisory-side for a human/upstream), and
  fail-open so it never breaks execution.
- Surfaced via `GET /api/portfolio-risk` (advisory; `available:false` when
  disabled/unreadable). Config-driven (`config.yaml` `portfolio:` +
  `PORTFOLIO_*` env). 50 offline tests (`tests/test_portfolio_sizing.py`,
  `tests/test_risk_engine.py`, + dashboard-API and executor-gate cases).

## Phase 5: Strategy Evaluation and Automation

### Target files

- `src/eval/walkforward.py` (new)
- `src/eval/attribution.py` (new)
- `tests/test_eval_*.py` (new)

### Scope

- Walk-forward evaluation and out-of-sample gating.
- Transaction cost/slippage models per asset class.
- Champion/challenger workflow for promotion to paper/live.

## Phase 6: Operational Intelligence Layer

### Target files

- `dashboard.py` (model/strategy introspection views)
- `src/alerts/*` (risk/system alert routing)
- `src/ops/metrics.py` (new)

### Scope

- Structured metrics: latency, fill quality, risk breaches, PnL attribution.
- LLM research copilot for explanation and anomaly triage (read-only to execution).
- Incident-level audit trail for all strategy and risk decisions.

## Definition of Done (Per Phase)

- All new modules have unit tests and integration tests.
- Backtests reconcile accounting and risk constraints deterministically.
- API surfaces return sanitized errors only.
- Docs and `.env.example` are aligned with runtime behavior.
