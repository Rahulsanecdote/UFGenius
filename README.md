# UFGenius — Alpaca Signal Bot

> ⚠️ **DISCLAIMER**: Educational and informational use only. Not financial advice.

Autonomous stock scanner that generates BUY/SELL/HOLD signals, risk-aware trade plans, and portfolio-level backtesting.

## Architecture

```
bot.py                     ← CLI entry point
 dashboard.py              ← Local web dashboard + API
src/
├── core/                  ← Canonical typed models + provider contracts
├── data/                  ← Market/universe fetch with retry/cache
│   └── providers/         ← Provider adapters + default registry
├── features/              ← Feature registry/store + weighting policies
├── technical/             ← Trend/momentum/volatility/volume indicators
├── fundamental/           ← Fundamental fetch + scoring
├── sentiment/             ← News/social/insider sentiment
├── macro/                 ← Market regime detection
├── signals/               ← Signal context, filters, scoring, trade plans
├── scanner/               ← Universe scan orchestration
├── alerts/                ← Telegram/email notifications
├── backtest/              ← Portfolio backtesting engine (daily MTM)
├── alpaca/                ← Read-only Alpaca portfolio integration
└── utils/                 ← Config, logging, HTTP retry/session
```

## Roadmap

The phased implementation plan for evolving this into a universal multi-asset
analysis engine is documented here:

- `docs/universal_engine_roadmap.md`

## Reproducible Setup

### Option A: exact lock install (recommended)

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
```

### Option B: constrained development install

```bash
python -m pip install -r requirements.txt -c constraints.txt
```

## Configuration

```bash
cp .env.example .env
```

Set API keys as needed. Core environment controls include:

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`
- `REQUEST_*` and `YFINANCE_TIMEOUT_SEC` for retry/timeout behavior
- `FEATURE_*` for feature-store TTL/version and optional regime-aware weighting
- `DASHBOARD_*` for binding/auth/rate limiting

## CLI Usage

```bash
python bot.py --mode scan --ticker AAPL
python bot.py --mode scan --universe SP500
python bot.py --mode paper
python bot.py --mode live
python bot.py --mode backtest --start 2022-01-01 --end 2023-12-31
python bot.py --mode portfolio
```

## Dashboard Usage

```bash
python dashboard.py
```

Default bind is `127.0.0.1:5001`.

Remote exposure is disabled by default. For a public single-host deployment:

- set `DASHBOARD_ALLOW_REMOTE=true`
- set `DASHBOARD_API_KEY` or `DASHBOARD_API_KEYS` (comma-separated)
- external API clients can send either:
- `Authorization: Bearer <key>`
- `X-API-Key: <key>`
- the built-in browser dashboard now uses a short-lived signed same-origin token automatically, so the frontend still works when remote mode is enabled
- keep `DASHBOARD_RATE_LIMIT_BACKEND=sqlite` for multi-process shared-store throttling
- configure `DASHBOARD_RATE_LIMIT_DB_PATH` on persistent storage

Built-in API protections:

- strict ticker/account-size validation
- sanitized 4xx/5xx errors (no internal exception leakage)
- per-IP rate limiting (SQLite shared store by default)
- short-lived signed browser token for same-origin dashboard requests in remote mode

## Single-Host Deployment

This repository is now prepared to deploy the whole app on one Python host.

Included deployment files:

- `wsgi.py` for WSGI hosts
- `Procfile` for hosts that read process types
- `render.yaml` for Render blueprint deploys

Recommended production start command:

```bash
gunicorn --bind 0.0.0.0:$PORT wsgi:app
```

Recommended environment for a public deployment:

- `DASHBOARD_ALLOW_REMOTE=true`
- `DASHBOARD_API_KEY=<strong random secret>`
- `DASHBOARD_TRUST_PROXY=true`
- host-managed `PORT` value

Health check endpoint:

- `/healthz`

## Backtest Model

Backtest now uses portfolio-level accounting with:

- true entry and exit timestamps
- daily marked-to-market equity curve
- forced end-date closure for open positions
- max concurrent position enforcement
- reconciled cash + unrealized PnL + realized PnL

## Testing

```bash
python -m pytest tests/test_technical.py tests/test_signals.py tests/test_fundamental.py tests/test_backtest.py tests/test_scanner.py tests/test_dashboard_api.py tests/test_data_fetcher.py tests/test_security.py tests/test_provider_consistency.py tests/test_phase2_providers.py tests/test_phase3_features.py -v
python -m pytest tests -m integration -v
```

## Risk Controls

- 1% risk-per-trade sizing
- max position cap
- hard disqualifiers (penny stock, illiquidity, bankruptcy risk, micro-cap, chaser trap)
- regime-aware sizing multipliers

## Notes

- External sentiment/data APIs degrade gracefully when credentials are missing.
- Market data and ticker metadata fetching use retries and process-local caches.
