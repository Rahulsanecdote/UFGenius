# UFGenius — Alpaca Signal Bot

> ⚠️ **DISCLAIMER**: Educational and informational use only. Not financial advice.

Autonomous stock scanner that generates BUY/SELL/HOLD signals, risk-aware trade plans, and portfolio-level backtesting. Supports live order execution via Alpaca with partial-exit tracking, multi-source sentiment analysis, and a local web dashboard.

## Architecture

```
bot.py                     ← CLI entry point
dashboard.py               ← Local web dashboard + API
diagnose.py                ← Pipeline diagnostics tool
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
├── alpaca/                ← Alpaca broker: portfolio, orders, execution, position tracking
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

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER` — Alpaca broker credentials
- `REQUEST_*` and `YFINANCE_TIMEOUT_SEC` — retry/timeout behavior
- `ALLOW_PENNY_STOCKS` and `SIGNAL_MIN_PRICE` — minimum price hard filter
- `FEATURE_*` — feature-store TTL/version and optional regime-aware weighting
- `DASHBOARD_*` — binding, auth, and rate limiting
- `SECRET_KEY` — signing key for dashboard same-origin tokens

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
- the built-in browser dashboard uses a short-lived signed same-origin token automatically, so the frontend still works when remote mode is enabled
- keep `DASHBOARD_RATE_LIMIT_BACKEND=sqlite` for multi-process shared-store throttling
- configure `DASHBOARD_RATE_LIMIT_DB_PATH` on persistent storage

Built-in API protections:

- strict ticker/account-size validation
- sanitized 4xx/5xx errors (no internal exception leakage)
- per-IP rate limiting (SQLite shared store by default)
- short-lived signed browser token for same-origin dashboard requests in remote mode

## Live Order Execution

The bot supports live and paper trading via Alpaca:

- **Executor** (`src/alpaca/executor.py`) — submits market/limit orders with retry logic and thread safety
- **Orders** (`src/alpaca/orders.py`) — order lifecycle management and status tracking
- **Position Tracker** (`src/alpaca/position_tracker.py`) — tracks open positions and partial exits
- **Portfolio** (`src/alpaca/portfolio.py`) — read-only portfolio summary and P&L

Enable live execution with `--mode live`. Paper trading is the default (`ALPACA_PAPER=true`).

## Single-Host Deployment

Included deployment files:

- `wsgi.py` — for WSGI hosts
- `Procfile` — for hosts that read process types
- `render.yaml` — for Render blueprint deploys

Recommended production start command:

```bash
gunicorn --bind 0.0.0.0:$PORT wsgi:app
```

Recommended environment for a public deployment:

- `DASHBOARD_ALLOW_REMOTE=true`
- `DASHBOARD_API_KEY=<strong random secret>`
- `DASHBOARD_TRUST_PROXY=true`
- `SECRET_KEY=<strong random secret>`
- host-managed `PORT` value

Health check endpoint: `/healthz`

## Backtest Model

Backtest uses portfolio-level accounting with:

- true entry and exit timestamps
- daily marked-to-market equity curve
- forced end-date closure for open positions
- max concurrent position enforcement
- reconciled cash + unrealized PnL + realized PnL

## Testing

Run the full unit test suite:

```bash
python -m pytest tests/ -v
```

Run specific modules:

```bash
python -m pytest tests/test_technical.py tests/test_signals.py tests/test_fundamental.py -v
python -m pytest tests/test_backtest.py tests/test_scanner.py tests/test_portfolio.py -v
python -m pytest tests/test_sentiment_news.py tests/test_sentiment_social.py tests/test_sentiment_insider.py -v
python -m pytest tests/test_security.py tests/test_position_tracker.py tests/test_universe.py -v
```

Run integration tests only:

```bash
python -m pytest tests/ -m integration -v
```

## Diagnostics

Use `diagnose.py` to inspect pipeline health, provider availability, and scan output:

```bash
python diagnose.py
```

## Risk Controls

- 1% risk-per-trade sizing
- max position cap
- hard disqualifiers (penny stock, illiquidity, bankruptcy risk, micro-cap, chaser trap)
- regime-aware sizing multipliers

## Notes

- External sentiment/data APIs degrade gracefully when credentials are missing.
- Market data and ticker metadata fetching use retries and process-local caches.
- Thread safety is enforced across the monitor, executor, and position tracker.
