"""Configuration loader — reads config.yaml and .env."""

import math
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Locate project root (two levels above this file)
_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = _ROOT / ".env"
_CONFIG_FILE = _ROOT / "config.yaml"

load_dotenv(_ENV_FILE)


def _load_yaml() -> dict:
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg: dict = _load_yaml()


def get(key: str, default: Any = None) -> Any:
    """Dot-notation access into config, e.g. get('safety_rules.max_positions')."""
    parts = key.split(".")
    val = _cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return default
    return val if val is not None else default


def env(key: str, default: str = "") -> str:
    """Fetch an environment variable, with optional default."""
    return os.getenv(key, default)


def _as_int(value: Any, default: int) -> int:
    """Coerce an arbitrary value to int, falling back to default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


# Convenience accessors
ACCOUNT_SIZE: float = float(get("account_size", 10_000))
RISK_PER_TRADE: float = float(get("risk_per_trade", 0.01))
MAX_POSITION_PCT: float = float(get("max_position_pct", 0.10))
SCAN_UNIVERSE: str = get("scan_universe", "SP500")
ATR_STOP_MULTIPLIER: float = float(get("atr_stop_multiplier", 2.0))
TARGET_RR_RATIOS: list = get("target_rr_ratios", [1.5, 2.5, 4.0])
TARGET_EXIT_PCTS: list = get("target_exit_pcts", [30, 40, 30])

# Disqualification-filter thresholds (read from config.yaml `filter_*` keys).
# Previously hardcoded in src/signals/filters.py (audit H4).
FILTER_MIN_AVG_VOLUME: float = float(get("filter_min_avg_volume", 100_000))
FILTER_MIN_MARKET_CAP: float = float(get("filter_min_market_cap", 100_000_000))
FILTER_MAX_5DAY_GAIN_PCT: float = float(get("filter_max_5day_gain_pct", 50.0))
FILTER_BANKRUPTCY_Z: float = float(get("filter_bankruptcy_z", 1.0))

# Signal classification thresholds (score -> label, confidence).
# Read by src/signals/generator.py. Restored alongside the module that
# consumes it (was orphaned when generator.py was deleted).
SIGNAL_THRESHOLDS: list = get("signal_thresholds", [
    [80, "STRONG_BUY",  "VERY_HIGH"],
    [65, "BUY",         "HIGH"],
    [50, "WEAK_BUY",    "MODERATE"],
    [40, "HOLD",        "LOW"],
    [25, "WEAK_SELL",   "MODERATE"],
    [10, "SELL",        "HIGH"],
    [0,  "STRONG_SELL", "VERY_HIGH"],
])

# Expected-value parameters (historical backtested estimates).
EV_WIN_RATE: float = float(get("ev_win_rate", 0.45))
EV_AVG_RR:   float = float(get("ev_avg_rr", 2.5))

# T1 resistance-snap discount.
RESISTANCE_SNAP_DISCOUNT: float = env_float(
    "RESISTANCE_SNAP_DISCOUNT", float(get("resistance_snap_discount", 0.995))
)

# Phase 3 feature store.
FEATURE_CACHE_TTL_SEC: int = env_int("FEATURE_CACHE_TTL_SEC", 300)
FEATURE_CACHE_MAX_ENTRIES: int = env_int("FEATURE_CACHE_MAX_ENTRIES", 2000)
FEATURE_CACHE_VERSION: str = env("FEATURE_CACHE_VERSION", "v1")
FEATURE_ENABLE_REGIME_WEIGHTING: bool = env_bool("FEATURE_ENABLE_REGIME_WEIGHTING", False)

# Live position store path (used by src/alpaca/position_tracker.py).
LIVE_POSITION_STORE_PATH: str = env(
    "LIVE_POSITION_STORE_PATH",
    str(_ROOT / "data" / "live_positions.json"),
)

# Position-monitor poll interval in minutes (used by src/alpaca/executor.py).
# Clamped to a positive floor at use; declared here so it is actually configurable.
MONITOR_INTERVAL_MIN: int = env_int(
    "MONITOR_INTERVAL_MIN", _as_int(get("monitor_interval_min", 5), 5)
)

# Backtest cost model (audit H5). Per-side commission and slippage as fractions
# of notional (0.001 = 0.1%). Defaults are conservative retail estimates.
BACKTEST_COMMISSION_PCT: float = env_float(
    "BACKTEST_COMMISSION_PCT", float(get("commission_pct", 0.001))
)
BACKTEST_SLIPPAGE_PCT: float = env_float(
    "BACKTEST_SLIPPAGE_PCT", float(get("slippage_pct", 0.001))
)

def _finite_or(value: float, default: float) -> float:
    """Reject NaN/inf from env parsing — a NaN would sail through min/max
    clamps (every comparison is False) and break the consumer's guard."""
    return value if math.isfinite(value) else default


# Cache eviction trims to this fraction of the size limit (avoids re-sweeping
# on every write right at the threshold). Effective range is [0.1, 1.0]:
# out-of-range values are clamped, non-finite values fall back to 0.8.
_CACHE_EVICTION_DEFAULT = 0.8
_CACHE_EVICTION_BOUNDS = (0.1, 1.0)
CACHE_EVICTION_TARGET_RATIO: float = _finite_or(
    env_float("CACHE_EVICTION_TARGET_RATIO", _CACHE_EVICTION_DEFAULT),
    _CACHE_EVICTION_DEFAULT,
)


def cache_eviction_target_ratio() -> float:
    """The normalized eviction trim ratio: finite, clamped to [0.1, 1.0].

    Single home for the fallback and bounds so the env docs, config value,
    and sweep behavior cannot drift apart (consumed by src/data/cache.py).
    Reads the module attribute at call time so tests can monkeypatch it.
    """
    lo, hi = _CACHE_EVICTION_BOUNDS
    ratio = _finite_or(float(CACHE_EVICTION_TARGET_RATIO), _CACHE_EVICTION_DEFAULT)
    return min(max(ratio, lo), hi)

# User-Agent sent with constituent-list fetches (Wikipedia / iShares).
# `or` fallback: an empty env value (e.g. a copied .env.example line) must not
# strip the UA — some hosts reject requests without one.
CONSTITUENT_FETCH_USER_AGENT: str = (
    env("CONSTITUENT_FETCH_USER_AGENT", "").strip()
    or "UFGenius/1.0 (+https://github.com/Rahulsanecdote/UFGenius)"
)

# Optional point-in-time universe membership file (audit M11). When set, the
# backtest only takes entries in tickers that were members of the universe on
# the entry date, correcting survivorship bias in the run-time ticker list.
# Empty/unset keeps the current behavior plus its survivorship disclosure.
BACKTEST_UNIVERSE_HISTORY_PATH: str | None = env(
    "BACKTEST_UNIVERSE_HISTORY_PATH", get("universe_history_path", None)
)

SIGNAL_WEIGHTS: dict = get("signal_weights", {
    "technical": 0.35,
    "volume": 0.20,
    "sentiment": 0.20,
    "fundamental": 0.15,
    "macro": 0.10,
})

SAFETY: dict = get("safety_rules", {
    "max_positions": 5,
    "max_portfolio_risk_pct": 5.0,
    "max_daily_loss_pct": 2.0,
    "cash_reserve_pct": 20.0,
    "min_market_cap": 300_000_000,
    "min_daily_volume": 200_000,
    "max_single_position_pct": 10.0,
    "max_trades_per_day": 3,
    "trade_in_bear_market": False,
})

# Strategy edge-validation gates (bot.py --mode validate, upgrade plan P0.1).
_VALIDATION: dict = get("validation", {})
VALIDATION_OOS_SHARPE_FLOOR: float = float(_VALIDATION.get("oos_sharpe_floor", 1.0))
VALIDATION_BOOTSTRAP_SHARPE_P05_FLOOR: float = float(
    _VALIDATION.get("bootstrap_sharpe_p05_floor", 0.0)
)
VALIDATION_PROB_PROFITABLE_FLOOR: float = float(_VALIDATION.get("prob_profitable_floor", 0.60))
VALIDATION_WINDOW_PROFITABLE_FRACTION_FLOOR: float = float(
    _VALIDATION.get("window_profitable_fraction_floor", 0.60)
)
VALIDATION_MIN_OOS_TRADES: int = int(_VALIDATION.get("min_oos_trades", 20))
VALIDATION_MIN_OOS_DAYS: int = int(_VALIDATION.get("min_oos_days", 30))

# API keys
NEWSAPI_KEY: str = env("NEWSAPI_KEY")
ALPHA_VANTAGE_KEY: str = env("ALPHA_VANTAGE_KEY")
POLYGON_KEY: str = env("POLYGON_KEY")
FMP_KEY: str = env("FMP_KEY")
FINNHUB_KEY: str = env("FINNHUB_KEY")
FRED_API_KEY: str = env("FRED_API_KEY")
REDDIT_CLIENT_ID: str = env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET: str = env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT: str = env("REDDIT_USER_AGENT", "StockBot/1.0 by u/yourusername")
TELEGRAM_BOT_TOKEN: str = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = env("TELEGRAM_CHAT_ID")
EMAIL_FROM: str = env("EMAIL_FROM")
EMAIL_PASSWORD: str = env("EMAIL_PASSWORD")
EMAIL_TO: str = env("EMAIL_TO")

# Alpaca Official API
ALPACA_API_KEY: str = env("ALPACA_API_KEY")
ALPACA_SECRET_KEY: str = env("ALPACA_SECRET_KEY")
ALPACA_PAPER: bool = env_bool("ALPACA_PAPER", True)

# Network hardening
REQUEST_TIMEOUT_SEC: float = env_float("REQUEST_TIMEOUT_SEC", 10.0)
REQUEST_CONNECT_TIMEOUT_SEC: float = env_float("REQUEST_CONNECT_TIMEOUT_SEC", 5.0)
REQUEST_MAX_RETRIES: int = env_int("REQUEST_MAX_RETRIES", 3)
REQUEST_BACKOFF_SEC: float = env_float("REQUEST_BACKOFF_SEC", 0.5)
REQUEST_POOL_SIZE: int = env_int("REQUEST_POOL_SIZE", 20)
YFINANCE_TIMEOUT_SEC: float = env_float("YFINANCE_TIMEOUT_SEC", 15.0)

# Dashboard hardening
DASHBOARD_HOST: str = env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = env_int("DASHBOARD_PORT", 5001)
DASHBOARD_ALLOW_REMOTE: bool = env_bool("DASHBOARD_ALLOW_REMOTE", False)
DASHBOARD_API_KEY: str = env("DASHBOARD_API_KEY")
DASHBOARD_API_KEYS: str = env("DASHBOARD_API_KEYS")
DASHBOARD_RATE_LIMIT_PER_MIN: int = env_int("DASHBOARD_RATE_LIMIT_PER_MIN", 60)
DASHBOARD_MAX_ACCOUNT_SIZE: float = env_float("DASHBOARD_MAX_ACCOUNT_SIZE", 10_000_000.0)
DASHBOARD_MIN_ACCOUNT_SIZE: float = env_float("DASHBOARD_MIN_ACCOUNT_SIZE", 100.0)
DASHBOARD_RATE_LIMIT_BACKEND: str = env("DASHBOARD_RATE_LIMIT_BACKEND", "sqlite")
DASHBOARD_RATE_LIMIT_DB_PATH: str = env("DASHBOARD_RATE_LIMIT_DB_PATH", "/tmp/ufgenius_rate_limit.sqlite3")
DASHBOARD_TRUST_PROXY: bool = env_bool("DASHBOARD_TRUST_PROXY", False)

# Penny stock mode
ALLOW_PENNY_STOCKS: bool = env_bool("ALLOW_PENNY_STOCKS", False)
SIGNAL_MIN_PRICE: float = env_float("SIGNAL_MIN_PRICE", 1.0)
CUSTOM_WATCHLIST: str = env("CUSTOM_WATCHLIST", "")

# Provider concurrency
PROVIDER_CONCURRENCY_LIMIT: int = env_int("PROVIDER_CONCURRENCY_LIMIT", 10)
