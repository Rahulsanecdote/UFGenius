# CLAUDE.md — UFGenius AI Assistant Guide

## Project Overview

**UFGenius** (Universal Financial Genius) is a full-stack algorithmic trading platform. It provides:
- Real-time market data monitoring and charting
- Technical indicator calculations (RSI, MACD, Bollinger Bands, SMA, EMA, ATR)
- Trading strategy implementation and backtesting
- Risk management with kill-switch, drawdown limits, and position sizing
- WebSocket-based real-time signal streaming

---

## Repository Structure

```
UFGenius/
├── CLAUDE.md                      # This file
├── README.md                      # Minimal project readme
├── LICENSE                        # MIT License
├── backend/                       # Python/FastAPI backend
│   ├── requirements.txt
│   └── app/
│       ├── main.py                # FastAPI app entry point
│       ├── api/
│       │   └── routes.py          # All REST API endpoints
│       ├── core/
│       │   ├── config.py          # Pydantic settings (reads .env)
│       │   └── database.py        # Async SQLAlchemy + PostgreSQL setup
│       ├── models/
│       │   └── models.py          # ORM models: Position, Order, TradeSignal, BacktestResult
│       ├── schemas/
│       │   └── schemas.py         # Pydantic request/response schemas
│       ├── services/
│       │   ├── backtester.py      # Event-driven backtest engine
│       │   ├── data_service.py    # Market data retrieval
│       │   ├── indicators.py      # Technical indicator calculations
│       │   ├── risk_manager.py    # Risk management system
│       │   └── websocket_manager.py # WebSocket connection management
│       └── strategies/
│           ├── base.py            # Abstract base class + strategy registry
│           └── impl.py            # Strategy implementations (e.g., RSI Mean Reversion)
└── ufgenius-frontend/             # React/TypeScript frontend
    ├── package.json
    ├── pnpm-lock.yaml
    ├── vite.config.ts
    ├── tailwind.config.js
    ├── tsconfig.json
    └── src/
        ├── main.tsx               # React app entry point
        ├── App.tsx                # Root component, tab routing, global state
        ├── components/
        │   ├── BacktestForm.tsx   # Backtest parameter form
        │   ├── BacktestResults.tsx# Results metrics + equity curve chart
        │   ├── Charts.tsx         # Price history charts (Recharts)
        │   ├── ErrorBoundary.tsx  # React error boundary
        │   ├── MarketData.tsx     # Watchlist + live prices
        │   ├── RiskStatus.tsx     # Kill switch + risk metrics display
        │   ├── Sidebar.tsx        # Navigation sidebar
        │   └── Signals.tsx        # Trading signals list
        ├── hooks/
        │   └── use-mobile.tsx     # Mobile viewport detection
        └── lib/
            ├── api.ts             # Axios client + all API endpoint wrappers
            ├── store.ts           # Global state types and useStore hook
            └── utils.ts           # Shared utility functions
```

---

## Tech Stack

### Backend
| Layer | Technology |
|---|---|
| Framework | FastAPI 0.109 |
| Language | Python 3 |
| ORM | SQLAlchemy 2.0 (async) |
| Database | PostgreSQL via asyncpg |
| Validation | Pydantic v2 |
| Data processing | Pandas, NumPy |
| Technical indicators | TA-Lib, Pandas-TA |
| Real-time | WebSockets |
| Logging | Loguru |
| Migrations | Alembic |
| Testing | pytest, pytest-asyncio, pytest-cov |

### Frontend
| Layer | Technology |
|---|---|
| Framework | React 18 + TypeScript |
| Build tool | Vite 6 |
| Styling | Tailwind CSS 3 |
| Components | Radix UI (29 packages) |
| Charting | Recharts 2 |
| HTTP client | Axios |
| Forms | React Hook Form + Zod |
| Routing | React Router 6 |
| Icons | Lucide React |
| Testing | Playwright |
| Package manager | pnpm |

---

## Development Workflows

### Backend Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # Edit with real credentials
```

**Required `.env` variables:**
```
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/ufgenius
REDIS_URL=redis://localhost:6379
API_KEY=your_api_key
LOG_LEVEL=INFO
ALPHA_VANTAGE_API_KEY=your_key
FINNHUB_API_KEY=your_key
```

**Run the backend:**
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs available at: `http://localhost:8000/docs` (Swagger) and `/redoc`

### Frontend Setup

```bash
cd ufgenius-frontend
pnpm install
```

**Optional `.env` override:**
```
VITE_API_URL=http://localhost:8000/api/v1
```

**Development scripts:**
```bash
pnpm dev          # Start dev server (auto-installs deps)
pnpm build        # TypeScript check + production build
pnpm build:prod   # Production build with BUILD_MODE=prod
pnpm lint         # ESLint check
pnpm preview      # Preview production build
pnpm clean        # Wipe node_modules and lock file
```

### Running Tests

**Backend:**
```bash
cd backend
pytest                         # Run all tests
pytest --cov=app               # With coverage report
pytest -v                      # Verbose output
```

**Frontend:**
```bash
cd ufgenius-frontend
npx playwright test            # E2E tests (requires Playwright config)
```

> **Note:** Test frameworks are installed but no test files exist yet. Creating tests is a high-priority contribution area.

---

## Architecture and Key Conventions

### Backend Conventions

**Project layout follows domain-driven separation:**
- `api/routes.py` — thin route handlers; delegate to services
- `services/` — all business logic lives here
- `models/` — SQLAlchemy ORM models only (no logic)
- `schemas/` — Pydantic input/output validation only
- `strategies/` — strategy implementations inherit from `base.py`

**All database operations are async.** Use `async def` + `await` for any DB, IO, or WebSocket calls.

**Configuration is centralized** in `app/core/config.py` via Pydantic `Settings`. Never hardcode values — read from settings or environment.

**Default trading configuration values (from `config.py`):**
- Initial capital: $100,000
- Commission rate: 0.1%
- Slippage: 0.1%
- Max position size: 10% of capital
- Max drawdown limit: 15%
- Daily loss limit: 5%

**API base path:** `/api/v1`

**CORS:** Currently set to allow all origins (`*`). Restrict in production.

### Adding a New Trading Strategy

1. Create a class in `app/strategies/impl.py` that inherits from `BaseStrategy` (`app/strategies/base.py`).
2. Implement the required abstract methods: `generate_signals()` and `get_default_parameters()`.
3. Register it with the strategy registry in `base.py`.
4. Add corresponding Pydantic schema in `schemas/schemas.py` if new parameters are needed.
5. The strategy automatically becomes available via the `GET /api/v1/strategies` endpoint.

### Adding a New Technical Indicator

1. Add the calculation function in `app/services/indicators.py` using Pandas/NumPy (vectorized).
2. Add a route in `app/api/routes.py` following the existing pattern.
3. Add the corresponding API call in `src/lib/api.ts` on the frontend.

### Frontend Conventions

**State management:** All global application state is typed in `src/lib/store.ts`. The `useStore` hook is the single source of truth.

**API calls:** All HTTP calls go through `src/lib/api.ts`. The Axios client is pre-configured with the base URL. Group new endpoints into the appropriate domain export (`marketApi`, `indicatorsApi`, etc.).

**Styling:** Use Tailwind CSS utility classes. Custom theme colors are defined in `tailwind.config.js`:
- Primary: `#2B5D3A` (dark green)
- Secondary: `#4A90E2` (blue)
- Accent: `#F5A623` (orange)

**Component patterns:**
- Keep components in `src/components/`
- Each component handles one UI concern
- Use `ErrorBoundary.tsx` to wrap async/data-driven sections
- Use `use-mobile.tsx` hook for responsive behavior

**TypeScript:** Strict mode is enabled. No `any` types — define proper interfaces in `store.ts` or inline.

**Path alias:** `@/*` maps to `src/*` — use this for all internal imports.

---

## API Reference Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/market/prices` | Current prices (multi-symbol) |
| GET | `/api/v1/market/history/{symbol}` | Historical OHLCV data |
| GET | `/api/v1/market/symbols` | Available symbols |
| GET | `/api/v1/indicators/rsi` | RSI calculation |
| GET | `/api/v1/indicators/macd` | MACD calculation |
| GET | `/api/v1/indicators/bollinger` | Bollinger Bands |
| GET | `/api/v1/indicators/sma` | Simple Moving Average |
| GET | `/api/v1/indicators/ema` | Exponential Moving Average |
| GET | `/api/v1/indicators/atr` | Average True Range |
| GET | `/api/v1/strategies` | List all strategies |
| GET | `/api/v1/strategies/{name}` | Strategy details and parameters |
| POST | `/api/v1/backtest/run` | Execute backtest |
| GET | `/api/v1/backtest/{id}` | Get backtest result |
| GET | `/api/v1/risk/status` | Risk metrics |
| POST | `/api/v1/risk/kill-switch` | Activate/deactivate kill switch |
| POST | `/api/v1/risk/reset-daily` | Reset daily P&L counter |
| GET | `/api/v1/signals` | Active trading signals |
| GET/POST/DELETE | `/api/v1/watchlist` | Watchlist CRUD |

---

## Database Models

| Model | Key Fields |
|---|---|
| `Position` | symbol, entry_price, exit_price, quantity, side, pnl, status |
| `Order` | symbol, type (MARKET/LIMIT/STOP/STOP_LIMIT), side, quantity, price, status |
| `TradeSignal` | symbol, signal_type (BUY/SELL/HOLD), strength, price, indicators |
| `BacktestResult` | strategy, symbol, start_date, end_date, metrics (JSON), trades (JSON) |

---

## Backtest Metrics

The `backtester.py` engine calculates:
- Total return and annualized CAGR
- Sharpe ratio and Sortino ratio
- Maximum drawdown
- Win rate, profit factor
- Average win / average loss
- Full equity curve (for charting)

---

## Risk Management System

`risk_manager.py` enforces:
- **Kill switch**: Halts all trading when triggered
- **Daily loss limit**: 5% of capital (configurable)
- **Max drawdown**: 15% from peak (configurable)
- **Position sizing**: Risk-based calculation per trade
- **Exposure limits**: Max open positions and total exposure tracking

---

## Known Gaps / Areas for Contribution

- **No tests exist** — both pytest (backend) and Playwright (frontend) are installed but empty
- **No Docker/CI configuration** — containerization and CI/CD pipeline not yet set up
- **CORS is open** — must be restricted before production deployment
- **No authentication** — API has no auth layer beyond `API_KEY` config
- **README is minimal** — should be expanded with setup and architecture docs
- **Alembic migrations not initialized** — database schema changes need migration setup

---

## Git Workflow

- Default branch: `master`
- Feature branches follow the pattern: `claude/<feature-name>-<id>`
- Commit messages should be descriptive and imperative: `Add RSI divergence strategy`, `Fix kill switch reset bug`
