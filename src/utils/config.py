"""Configuration loader — reads config.yaml and .env."""

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

# Phase 3 feature store
FEATURE_CACHE_TTL_SEC: int = env_int("FEATURE_CACHE_TTL_SEC", 300)
FEATURE_CACHE_MAX_ENTRIES: int = env_int("FEATURE_CACHE_MAX_ENTRIES", 2000)
FEATURE_CACHE_VERSION: str = env("FEATURE_CACHE_VERSION", "v1")
FEATURE_ENABLE_REGIME_WEIGHTING: bool = env_bool("FEATURE_ENABLE_REGIME_WEIGHTING", False)
