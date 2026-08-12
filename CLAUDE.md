# CLAUDE.md — UFGenius AI Assistant Guide

## Project Overview

**UFGenius** is an autonomous **stock signal bot**. It scans an equity universe,
scores each ticker across technical / volume / sentiment / fundamental / macro
dimensions, emits BUY/SELL/HOLD signals with risk-aware trade plans, and can
place risk-gated orders through Alpaca. It ships a Flask web dashboard and a CLI.

> ⚠️ Educational use only. **Not financial advice.** Paper-trade before risking real money.

**Stack:** Python 3.12, Flask + gunicorn (dashboard/API), yfinance + optional
providers (Alpha Vantage / Polygon / Finnhub) for data, `alpaca-py` for the
broker, pandas/NumPy for computation, VADER + PRAW + NewsAPI for sentiment,
`fredapi` for macro. There is **no** React frontend, no FastAPI, and no
application database — the dashboard is a single self-contained HTML page
rendered via `render_template_string`, and state is file-/JSON-based. (A small
SQLite file backs only the dashboard rate limiter; there is no relational
data model.)

---

## Repository Structure

```
UFGenius/
├── bot.py                     ← CLI entry point (scan / paper / live / backtest / portfolio)
├── dashboard.py               ← Flask app: HTML dashboard + JSON API
├── wsgi.py / Procfile         ← gunicorn entry (wsgi:app) for production hosts
├── diagnose.py                ← pipeline diagnostics
├── config.yaml                ← strategy / risk / schedule config (non-secret)
├── .env(.example)             ← API keys + runtime tuning (secret; gitignored)
├── requirements.txt / .lock / constraints.txt
├── render.yaml                ← Render deployment (gunicorn + dashboard hardening)
├── src/
│   ├── core/          ← typed models (Instrument, TickerSnapshot, …) + provider contracts
│   ├── data/          ← OHLCV/universe fetch (retry/cache); providers/ adapters + registry
│   ├── features/      ← feature registry/store + weighting policies (Phase 3)
│   ├── technical/     ← trend, momentum, volatility, volume, support/resistance
│   ├── fundamental/   ← fundamentals fetch + scoring (Piotroski, Altman Z, valuation)
│   ├── sentiment/     ← news / social (Reddit) / insider sentiment
│   ├── macro/         ← market-regime detection (VIX, breadth, FRED)
│   ├── signals/       ← generator (composite score → signal), filters, context, trade_plan
│   ├── scanner/       ← daily_scan (universe orchestration) + gap_scanner
│   ├── screener/      ← named pre-trade filter presets (oversold-bounce/ma-bounce/breakout)
│   ├── alerts/        ← telegram / email notifications
│   ├── backtest/      ← portfolio backtest engine (daily MTM, commission + slippage)
│   ├── alpaca/        ← portfolio (read-only), orders, executor (+RiskGuard), position_tracker
│   ├── portfolio/     ← volatility/correlation-aware position sizing (advisory, Phase 4)
│   ├── risk/          ← portfolio-level risk engine (leverage/heat/cluster/drawdown; advisory, Phase 4)
│   └── utils/         ← config, logging, HTTP retry/session, dashboard security
└── tests/             ← pytest suite (unit + `integration`-marked network tests)
```

---

## Development Workflows

### Setup

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.lock       # reproducible install
# or: pip install -r requirements.txt -c constraints.txt
cp .env.example .env                             # fill in keys you need
```

Only `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are needed for portfolio/execution;
data works on yfinance alone. `ALPACA_PAPER=true` (default) keeps you on paper.

### CLI (`bot.py`)

```bash
python bot.py --mode scan                       # one full-universe scan
python bot.py --mode scan --ticker AAPL         # single ticker
python bot.py --mode screen --preset oversold-bounce  # filter a universe by a named screener preset → watchlist
python bot.py --mode paper                      # scheduled scans, log only
python bot.py --mode live                        # scheduled scans + alerts
python bot.py --mode live --execute             # + submit orders to the PAPER account
python bot.py --mode live --live-execute        # + REAL-MONEY orders (needs ALPACA_PAPER=false)
python bot.py --mode live --execute --dry-run   # preview orders, submit nothing
python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
python bot.py --mode intraday-backtest --entry breakout --interval 5m  # OOS check for the intraday entries (breakout / sweep_reclaim)
python bot.py --mode validate --start 2022-01-01 --end 2023-12-31  # walk-forward + OOS + bootstrap edge check (P0.1)
python bot.py --mode validate --save-baseline   # + persist the OOS metrics as the paper-vs-backtest reference
python bot.py --mode optimize --start 2022-01-01 --end 2023-12-31  # in-sample grid search + overfitting haircut + OOS confirm (P0.2)
python bot.py --mode portfolio                  # read-only Alpaca portfolio
python bot.py --mode intraday-scan              # continuous intraday candidate scan → queue (P1.2)
python bot.py --mode earnings-calendar          # build/refresh the earnings calendar (P1.4)
```

Execution safety: `--execute` targets the **paper** account and refuses to run
if `ALPACA_PAPER=false`; `--live-execute` is the only real-money path and
refuses unless `ALPACA_PAPER=false`. Every order passes `RiskGuard`
(`src/alpaca/executor.py`), which enforces the subset of `config.yaml`
`safety_rules` listed under Architecture & Conventions.

### Dashboard

```bash
python dashboard.py                     # local: http://127.0.0.1:5001
gunicorn --bind 0.0.0.0:$PORT wsgi:app  # production (see Procfile / render.yaml)
```

**Security:** when the app is network-exposed (`DASHBOARD_ALLOW_REMOTE=true` or
a `PORT` env var is present) it **requires** `DASHBOARD_API_KEY`/
`DASHBOARD_API_KEYS` and fails closed at startup without one. Send the key as
`X-API-Key: <key>` or `Authorization: Bearer <key>`. A rate limiter runs before
auth; behind a trusted proxy set `DASHBOARD_TRUST_PROXY=true`.

### Tests

```bash
pytest                 # unit tests (integration/network tests excluded by default)
pytest -m integration  # opt into the network-hitting tests
pytest --cov=src       # coverage
```

---

## Architecture & Conventions

- **Config is centralized** in `src/utils/config.py` — it reads `config.yaml`
  (non-secret strategy/risk knobs) and `.env` (secrets + tuning). Never hardcode
  thresholds; add a `config.X` accessor and a `config.yaml`/env key.
- **Data flow:** `scanner/daily_scan` → `signals/context` (one fetch per ticker
  via `data/providers`) → `technical`/`fundamental`/`sentiment`/`macro` scores →
  `signals/generator.generate_signal` (weighted composite → label via
  `SIGNAL_THRESHOLDS`) → `signals/filters` (hard disqualifiers) →
  `signals/trade_plan` (entry/stop/targets/sizing) → alerts / executor.
- **Money paths are gated:** sizing never forces a share it can't afford
  (returns a `skip` plan); `RiskGuard` (`src/alpaca/executor.py`) blocks entries
  that breach `safety_rules` — max positions, single-position cap, cash reserve,
  per-trade risk, daily trade count, bear-market, duplicate-ticker,
  `stop_loss_required`, daily/weekly realized-loss limits, post-loss cooldown,
  earnings-week (P1.4 **calendar-backed** via `src/catalysts/earnings_calendar.py`,
  yfinance fallback), the **P1.4 catalyst-tag veto** (`src/catalysts/catalyst_gate.py`
  — blocks entries whose `catalyst_tags` hit `catalysts.veto_tags`),
  `paper_trade_days_required` (live only — **P0.4** upgraded this from a tenure
  check to tenure **plus** a paper-scorecard performance gate: the realized
  paper trades must clear configured floors before real money; a second,
  **opt-in** half then requires those same paper metrics to stay **within
  tolerance of the validated out-of-sample backtest** —
  `src/backtest/baseline.py`, config `paper_scorecard.baseline_*`, reference
  saved by `--mode validate --save-baseline`. One-sided, so only paper
  *underperformance* blocks; fail-closed on a missing/stale/unvalidated
  baseline. Each half is separately enabled and the gate passes only if every
  enabled half passes), and the **P0.3 circuit breakers**
  (global operator halt, broker-error breaker, data-staleness breaker — checked
  first, block new entries only). Realized-loss limits and the cooldown read a
  realized-P&L ledger the monitor writes at each exit; the position tracker also
  writes a per-**trade** outcome ledger (P0.4) that `src/alpaca/scorecard.py`
  turns into backtest-comparable metrics for the live performance gate; the
  circuit-breaker state (halt flag + broker-error trail) is a JSON file shared
  between the dashboard and CLI (`src/alpaca/circuit_breaker.py`, config
  `circuit_breakers:`). Every fill's expected-vs-realized price is recorded to the
  P2.1 execution-quality ledger (`src/alpaca/execution_quality.py`) as adverse
  slippage + implementation shortfall; with `execution_quality:
  use_measured_slippage` the backtest cost model uses that **measured** slippage.
  P2.2 smart order handling (`src/alpaca/smart_orders.py`, config `smart_orders:`,
  default off) prices the entry as a marketable limit crossing the market by an
  offset tuned to that measured slippage. Live trading needs an explicit flag +
  `ALPACA_PAPER=false`.
- **Intraday data (P1.1):** `fetch_intraday()` (`src/data/fetcher.py`) is the
  entry point for 1m/5m/… bars — same provider abstraction as daily, but with a
  **boundary-aligned** intraday cache TTL (`intraday.cache_boundary_align`,
  default on — the cache expires just after the next bar boundary + a settle
  grace, so a just-closed bar is picked up on the next poll; `_ttl_for_interval`)
  and the look-ahead guards in `src/data/lookahead.py`
  (order/dedupe, drop future-labelled bars, `as_of` clamp, stale-frame check).
  Use it (not `fetch_ohlcv`) for anything real-time; daily bars still use
  `fetch_ohlcv`. Knobs live under `config.yaml` `intraday:` / `INTRADAY_*`.
- **Intraday scan → entry pipeline (P1.2/P1.3):** `--mode intraday-scan` runs a
  `ContinuousScanner` (`src/scanner/intraday_scan.py`) that scores live intraday
  bars for volume/momentum/breakout/gap and pushes deduped hits into a
  `CandidateQueue` (`candidate_queue.py`); an `IntradayConsumer`
  (`intraday_consumer.py`) drains it and runs the deterministic intraday entry
  evaluator (`src/signals/intraday_signal.py`: VWAP + opening-range breakout +
  volume, intraday-ATR stop via `src/technical/intraday_features.py`). Discovery
  + planning only — plans go to a pluggable sink (default log); execution reuses
  the gated `execute_trade_plan` path. Config: `continuous_scan:` / `intraday_signal:`
  (default **1m bars polled every 15s** — the `_MIN_INTERVAL_SEC` loop floor; with
  the boundary-aligned intraday cache the cache expires just after each bar closes,
  so reaction to a newly-closed bar tracks the ~15s poll cadence, one provider call
  per bar — still not sub-minute *resolution* (1m is the provider floor and the
  forming bar is dropped by the look-ahead guard); `dedup_ttl_sec` tracks ~1 bar so re-scored cached polls
  are suppressed while the next bar can still re-qualify).
  An **opt-in sweep-reclaim reversal** entry (`src/signals/sweep_reclaim.py`,
  config `sweep_reclaim:`, **default off**) is the counterpart to the breakout:
  it detects a sweep of a recent swing low (`lookback_bars`=15) + a reclaim close
  on volume (`reclaim_window_bars`=2), stops just below the swept wick, and builds
  the plan through the same `generate_trade_plan` money-path. When
  `SWEEP_RECLAIM_ENABLED`, the producer enqueues a structural `sweep` candidate
  (`sweep_reclaim_present`, a superset of graded entries) and the consumer runs
  the full grading only when the breakout doesn't fire; both are behind the flag,
  so off ⇒ intraday path unchanged. Timing hypothesis — `--mode validate` covers
  the daily composite only, so the intraday entries are checked out-of-sample by
  the separate **`--mode intraday-backtest`** harness
  (`src/backtest/intraday_engine.py`); still judge it on paper via
  `/api/paper-scorecard` + `/api/attribution` before real money. See
  `docs/SWEEP_RECLAIM.md`.
- **Intraday backtest harness (`--mode intraday-backtest`):** the out-of-sample
  check for the intraday entries (`src/backtest/intraday_engine.py`,
  `backtest_intraday`, config `intraday_backtest:`). Replays the breakout /
  sweep-reclaim evaluator bar-by-bar with **no look-ahead** (bar-T signal → bar
  T+1 **same-session** open fill), manages the position **intrabar** (stop-first,
  then T1/T2/T3 partials by the bar's high, same geometry as the live plan), and
  forces **flat at session end** (no overnight holds). Reuses the daily cost
  model; metrics are trade-based (expectancy in R, profit factor, win rate) plus
  a fixed-fractional-risk equity curve, and the acceptance check refuses a
  sub-`min_trades` sample. Honest about its biases (`bias_disclosures`:
  intrabar-ordering, no-concurrency, short/provider-dependent data). Necessary,
  not sufficient — still paper-trade after. See `docs/INTRADAY_BACKTEST.md`.
- **All network fetches** go through `src/utils/http.py` (timeouts + bounded
  retry), including the constituent-list fetches in `src/data/universe.py`
  (tables/headers are located by content, not position). `src/data/cache.py`
  is a TTL disk cache with atomic writes, a lock-guarded eviction sweep, and
  a stale-fallback path.
- **Observability (P2.3):** `src/observability/` is pure telemetry — it never
  gates or places an order. `metrics.py` (`MetricsLedger`, singleton
  `default_ledger()`, interprocess-`flock`ed writes like the breaker store)
  records one bounded JSON record per scan (latency, scanned/signal counts,
  buy-side label histogram, regime) and its `summary()` exposes avg/**p95**
  (nearest-rank) latency and a **data-gap** flag. Gap detection
  (`observability.data_gap_seconds`) is **default-disabled** — raw elapsed time
  can't tell an outage from a normal quiet period (overnight/weekends), so it's
  opt-in above the deployment's real cadence; active-outage detection is better
  driven by an external watchdog polling `/api/metrics`.
  `attribution.py` turns the P0.4 trade-outcome ledger into a per-signal-label
  scorecard. `alerting.py` sends opt-in operational alerts (breaker trips, data
  gaps) via `send_text_alert` — **default off** (`observability.alerts.enabled`),
  best-effort, never raises. `run_daily_scan` records each scan (best-effort);
  the executor alerts only on the broker-breaker false→true trip transition.
  Surfaced via `/api/metrics`, `/api/attribution`, and dashboard panels.
- **Explainability (P3.1):** `src/explain/narrative.py` is an *optional* LLM
  layer that turns the **verified quant snapshot** into a plain-English bull/bear
  read for the dashboard/alerts. It is **advisory only** — no import of the
  executor/broker, structurally firewalled from the money path, and never raises.
  `build_snapshot()` sends only **structured verified fields** (scores, levels,
  regime, our own reason strings) — never raw news/social text — and the system
  prompt treats the snapshot as inert data and forbids buy/sell advice
  (prompt-injection sandbox). Uses the **Anthropic SDK** (`claude-opus-5`
  default; `anthropic` is an **optional** dependency — `requirements-explain.txt`,
  lazy-imported, only needed when enabled).
  **Cost-capped and default off** (`explain.enabled`): per-call `max_tokens` at
  `effort: low`, bounded input, and an interprocess-`flock`ed per-day call cap
  (reserved only after a usable client exists). Needs `ANTHROPIC_API_KEY`.
  Surfaced via `GET /api/explain?ticker=…` and an on-demand dashboard panel.
- **Portfolio + Risk Engine (roadmap Phase 4):** `src/portfolio/` (pure
  numpy/pandas volatility/correlation-aware sizing) and `src/risk/engine.py`
  (`PortfolioRiskEngine` → `RiskDecision`: portfolio-level gross-leverage,
  single-name-weight, portfolio-heat, correlated-cluster, and drawdown checks)
  are an **advisory, default-off** layer that *complements* `RiskGuard`, never
  replaces it. Firewalled from the money path (no executor/broker import) and
  fail-open (any internal error → *approve*, so a bug can't silently block
  trading). Default `portfolio.enabled=false` makes it a no-op surfaced only via
  `GET /api/portfolio-risk`; the separate opt-in `portfolio.gate_entries` lets
  `execute_trade_plan` consult it **after** RiskGuard approves — veto-only
  (tighten, never loosen). Config `portfolio:` / `PORTFOLIO_*`.
- **Async?** No — this is a synchronous codebase (Flask sync views, thread-pool
  fan-out for scans). Do not introduce `async def` without cause.

### Adding a technical indicator
Add a vectorized function in the relevant `src/technical/*.py`, wire it into the
scorer that consumes it, and expose it through `signals/generator` if it should
affect the composite score. Add a dashboard route in `dashboard.py` only if the
UI needs it.

### Adding a config-driven threshold
Add the key to `config.yaml` (or `.env` for secrets/tuning), add a typed
accessor in `src/utils/config.py`, and read it at the point of use.

---

## Dashboard API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness check |
| GET | `/` | HTML dashboard |
| GET | `/api/diagnose` | Pipeline/connectivity diagnostics |
| GET | `/api/price-history` | OHLCV series for a ticker/range |
| GET | `/api/regime` | Current market regime |
| GET | `/api/scan-ticker?ticker=AAPL` | Single-ticker scan + trade plan |
| GET | `/api/scan` | Full-universe scan (slow: 60–120s) |
| GET | `/api/scan-gaps` | Pre-market gap scan |
| GET | `/api/scan-breakouts` | Volume-breakout scan |
| GET | `/api/paper-scorecard` | Paper-trading scorecard: backtest-comparable metrics on realized trades (P0.4), plus the paper-vs-validated-backtest tolerance comparison |
| GET | `/api/execution-quality` | Realized slippage / implementation shortfall from recorded fills (P2.1) |
| GET | `/api/metrics` | Scan metrics: latency (avg/p95), signal counts, data-gap state (P2.3) |
| GET | `/api/attribution` | Per-signal realized outcome (win rate / avg return / P&L) from the trade ledger (P2.3) |
| GET | `/api/explain?ticker=AAPL` | Optional AI bull/bear narrative for a ticker's verified signal — advisory only (P3.1) |
| GET | `/api/portfolio-risk` | Advisory portfolio-level risk snapshot: gross leverage / heat / per-name weights vs limits (roadmap Phase 4; `available:false` when disabled) |
| GET | `/api/breaker-state` | Circuit-breaker / kill-switch state (P0.3) |
| POST | `/api/breaker` | Flip the global halt switch (`{"action":"halt"\|"resume"}`) |
| POST | `/api/clear-cache` | Clear the market-data cache |

All `/api/*` routes are rate-limited, and authenticated when the app is
network-exposed (see Dashboard security above).

---

## Key config (`config.yaml`)

`account_size`, `risk_per_trade`, `max_position_pct`, `scan_universe`,
`signal_weights` (technical/volume/sentiment/fundamental/macro), `signal_thresholds`
(score→label), `atr_stop_multiplier`, `target_rr_ratios`/`target_exit_pcts`,
`filter_*` (disqualifier thresholds), and `safety_rules` (max positions,
loss/exposure limits, cooldowns) — `RiskGuard` enforces the position/exposure/
trade-count/bear-market/duplicate rules, plus stop-required, daily/weekly
realized-loss limits, post-loss cooldown, earnings-week (P1.4 calendar-backed),
the P1.4 catalyst-tag veto (`catalysts:`), and the live-only paper graduation
gate (tenure + `paper_scorecard:` floors + the opt-in `baseline_*` tolerance
check against the validated backtest).

**Penny mode** (`penny:` / `ALLOW_PENNY_STOCKS`, default off): an opt-in
low-price/small-cap profile that does **not** disable protection — it swaps the
standard disqualifiers (`src/signals/filters.py`) for penny-specific **hard
rails**: a dollar-volume floor (price × avg volume — the real liquidity gate), a
positive market-cap floor (not 0), a price band, a tighter chaser-trap, and the
bankruptcy check **stays on**. Scan/paper only until validated. See
`docs/PENNY_MODE.md`.

Backtest frictions are configurable via `commission_pct`/`slippage_pct`
(config.yaml) or `BACKTEST_COMMISSION_PCT`/`BACKTEST_SLIPPAGE_PCT` (env).
Entries fill at the **next bar's open** after a signal (no same-bar lookahead);
results include `cost_model` and `bias_disclosures` (survivorship / daily
granularity) so reported returns are read with the right caveats. Survivorship
bias can be corrected by supplying a point-in-time membership file via
`universe_history_path` (config.yaml) or `BACKTEST_UNIVERSE_HISTORY_PATH`
(env) — see `src/backtest/universe_history.py` for the JSON format; entries
are then gated by membership on the entry date.

---

## Git Workflow

- Default branch: `main`
- Feature branches: `claude/<feature-name>-<id>`
- Commit messages: descriptive, imperative (`Add RSI divergence to composite`,
  `Fix stop gap-through in backtest`).

## Known Gaps / Contribution Areas

- Backtest survivorship bias is corrected only when a point-in-time membership
  file is supplied (`universe_history_path`); without one it remains and is
  disclosed in results. Build one with
  `python -m src.backtest.build_universe_history` (reconstructs S&P 500
  membership from Wikipedia's constituents + changes tables; early history is
  floored at the oldest change date). Even with the file, price history for
  delisted names depends on the data provider (yfinance drops many).
- No auth layer beyond the dashboard API key; the CLI/broker path trusts local env.
  (The dashboard deliberately emits NO CORS headers — the API is same-origin
  only; the browser UI prompts for the API key and sends it as `X-API-Key`.)
- Alembic/DB not used — there is no relational database in this project.
