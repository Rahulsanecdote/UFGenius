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
