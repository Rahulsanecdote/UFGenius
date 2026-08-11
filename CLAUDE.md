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
│   ├── alerts/        ← telegram / email notifications
│   ├── backtest/      ← portfolio backtest engine (daily MTM, commission + slippage)
│   ├── alpaca/        ← portfolio (read-only), orders, executor (+RiskGuard), position_tracker
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
python bot.py --mode paper                      # scheduled scans, log only
python bot.py --mode live                        # scheduled scans + alerts
python bot.py --mode live --execute             # + submit orders to the PAPER account
python bot.py --mode live --live-execute        # + REAL-MONEY orders (needs ALPACA_PAPER=false)
python bot.py --mode live --execute --dry-run   # preview orders, submit nothing
python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
python bot.py --mode validate --start 2022-01-01 --end 2023-12-31  # walk-forward + OOS + bootstrap edge check (P0.1)
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
  paper trades must clear configured floors before real money), and the
  **P0.3 circuit breakers**
  (global operator halt, broker-error breaker, data-staleness breaker — checked
  first, block new entries only). Realized-loss limits and the cooldown read a
  realized-P&L ledger the monitor writes at each exit; the position tracker also
  writes a per-**trade** outcome ledger (P0.4) that `src/alpaca/scorecard.py`
  turns into backtest-comparable metrics for the live performance gate; the
  circuit-breaker state (halt flag + broker-error trail) is a JSON file shared
  between the dashboard and CLI (`src/alpaca/circuit_breaker.py`, config
  `circuit_breakers:`). Live trading needs an explicit flag + `ALPACA_PAPER=false`.
- **Intraday data (P1.1):** `fetch_intraday()` (`src/data/fetcher.py`) is the
  entry point for 1m/5m/… bars — same provider abstraction as daily, but with an
  interval-scaled cache TTL and the look-ahead guards in `src/data/lookahead.py`
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
  the gated `execute_trade_plan` path. Config: `continuous_scan:` / `intraday_signal:`.
- **All network fetches** go through `src/utils/http.py` (timeouts + bounded
  retry), including the constituent-list fetches in `src/data/universe.py`
  (tables/headers are located by content, not position). `src/data/cache.py`
  is a TTL disk cache with atomic writes, a lock-guarded eviction sweep, and
  a stale-fallback path.
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
| GET | `/api/paper-scorecard` | Paper-trading scorecard: backtest-comparable metrics on realized trades (P0.4) |
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
the P1.4 catalyst-tag veto (`catalysts:`), and paper-trading-tenure (live only).

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
