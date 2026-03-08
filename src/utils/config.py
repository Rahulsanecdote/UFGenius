"""Configuration loader — reads config.yaml and .env, with startup validation."""

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


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------
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
    "trade_earnings_week": False,
})

# API keys
NEWSAPI_KEY: str = env("NEWSAPI_KEY")
ALPHA_VANTAGE_KEY: str = env("ALPHA_VANTAGE_KEY")
POLYGON_KEY: str = env("POLYGON_KEY")
FMP_KEY: str = env("FMP_KEY")
FRED_API_KEY: str = env("FRED_API_KEY")
REDDIT_CLIENT_ID: str = env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET: str = env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT: str = env("REDDIT_USER_AGENT", "StockBot/1.0")
TELEGRAM_BOT_TOKEN: str = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = env("TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
def _validate_config() -> list[str]:
    """
    Validate config values. Returns a list of warning strings.
    Does NOT raise — bad config logs warnings and falls back to defaults.
    """
    warnings: list[str] = []

    # account_size
    if ACCOUNT_SIZE <= 0:
        warnings.append(f"account_size={ACCOUNT_SIZE} must be > 0")

    # risk_per_trade
    if not (0 < RISK_PER_TRADE <= 0.10):
        warnings.append(
            f"risk_per_trade={RISK_PER_TRADE} should be in (0, 0.10]; "
            f"values above 10% per trade are extremely dangerous"
        )

    # max_position_pct
    if not (0 < MAX_POSITION_PCT <= 1.0):
        warnings.append(f"max_position_pct={MAX_POSITION_PCT} must be in (0, 1]")

    # atr_stop_multiplier
    if ATR_STOP_MULTIPLIER <= 0:
        warnings.append(f"atr_stop_multiplier={ATR_STOP_MULTIPLIER} must be > 0")

    # signal_weights must sum to ~1.0
    weight_sum = sum(SIGNAL_WEIGHTS.values())
    if abs(weight_sum - 1.0) > 0.02:
        warnings.append(
            f"signal_weights sum to {weight_sum:.3f}, expected 1.0 — "
            f"scores will be mis-scaled"
        )

    # target_exit_pcts must sum to 100
    exit_sum = sum(TARGET_EXIT_PCTS)
    if exit_sum != 100:
        warnings.append(
            f"target_exit_pcts {TARGET_EXIT_PCTS} sum to {exit_sum}, expected 100"
        )

    # target_rr_ratios must be strictly ascending and positive
    if not all(r > 0 for r in TARGET_RR_RATIOS):
        warnings.append(f"target_rr_ratios {TARGET_RR_RATIOS} must all be > 0")
    elif TARGET_RR_RATIOS != sorted(TARGET_RR_RATIOS):
        warnings.append(
            f"target_rr_ratios {TARGET_RR_RATIOS} should be in ascending order"
        )

    # scan_universe
    valid_universes = {"SP500", "RUSSELL1000"}
    if SCAN_UNIVERSE not in valid_universes:
        warnings.append(
            f"scan_universe='{SCAN_UNIVERSE}' is not recognised; "
            f"valid options: {valid_universes}"
        )

    # safety sanity checks
    max_pos = SAFETY.get("max_positions", 5)
    if not isinstance(max_pos, int) or max_pos < 1:
        warnings.append(f"safety_rules.max_positions={max_pos} must be a positive integer")

    daily_loss = SAFETY.get("max_daily_loss_pct", 2.0)
    if daily_loss <= 0 or daily_loss > 20:
        warnings.append(
            f"safety_rules.max_daily_loss_pct={daily_loss} looks wrong; "
            f"typical range is 1-5%"
        )

    return warnings


def _run_validation() -> None:
    """Run validation at import time and log any issues."""
    try:
        # Import here to avoid circular import (logger → config → logger)
        import logging
        _log = logging.getLogger("src.utils.config")

        issues = _validate_config()
        if issues:
            _log.warning("Config validation found %d issue(s):", len(issues))
            for w in issues:
                _log.warning("  ⚠ %s", w)
        else:
            _log.debug("Config validation passed.")
    except Exception:
        pass  # Never crash on validation


_run_validation()
