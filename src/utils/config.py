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


def _resolve_root(path: str) -> str:
    """Resolve a relative configured path against the project root.

    Absolute paths pass through. Keeps a service and a manually-run command from
    reading/writing different files when launched from different working dirs.
    """
    return path if os.path.isabs(path) else str(_ROOT / path)


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


def _as_float(value: Any, default: float) -> float:
    """Coerce an arbitrary value to float, falling back to default on failure.

    Used for eagerly-evaluated YAML defaults: a null or non-numeric config value
    must not raise at import (``env_float`` only guards the env-var parse, not the
    default expression).
    """
    try:
        return float(value)
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

# Market-movers discovery (MOVERS universe / --mode movers).
_MOVERS: dict = get("movers", {})
MOVERS_SOURCES: list = list(_MOVERS.get("sources", ["gainers", "losers", "most_actives"]))
MOVERS_MIN_PRICE: float = _as_float(_MOVERS.get("min_price", 1.0), 1.0)
MOVERS_MAX_PRICE: float = _as_float(_MOVERS.get("max_price", 0), 0.0)
MOVERS_MIN_CHANGE_PCT: float = _as_float(_MOVERS.get("min_change_pct", 3.0), 3.0)
MOVERS_LIMIT: int = _as_int(_MOVERS.get("limit", 40), 40)
MOVERS_INCLUDE_SHORT: bool = bool(_MOVERS.get("include_short_setups", True))
# Phase 2 — intraday enrichment / early-momentum ranking.
MOVERS_ENRICH_INTRADAY: bool = bool(_MOVERS.get("enrich_intraday", True))
MOVERS_ENRICH_INTERVAL: str = str(_MOVERS.get("enrich_interval", "5m"))
MOVERS_ENRICH_MAX: int = _as_int(_MOVERS.get("enrich_max", 40), 40)
# Phase 3 — screener-style movers alerts (opt-in, default off).
_MOVERS_ALERTS: dict = _MOVERS.get("alerts", {}) if isinstance(_MOVERS, dict) else {}
MOVERS_ALERTS_ENABLED: bool = env_bool("MOVERS_ALERTS_ENABLED", bool(_MOVERS_ALERTS.get("enabled", False)))
MOVERS_ALERTS_MIN_SCORE: float = _as_float(_MOVERS_ALERTS.get("min_score", 70), 70.0)
MOVERS_ALERTS_REQUIRE_ENRICHED: bool = bool(_MOVERS_ALERTS.get("require_enriched", True))
MOVERS_ALERTS_DEDUP_TTL_SEC: float = _as_float(_MOVERS_ALERTS.get("dedup_ttl_sec", 1800), 1800.0)
MOVERS_ALERTS_MAX_PER_RUN: int = _as_int(_MOVERS_ALERTS.get("max_per_run", 10), 10)
# Phase 4 — post-open monitoring + invalidation.
_MOVERS_MONITOR: dict = _MOVERS.get("monitor", {}) if isinstance(_MOVERS, dict) else {}
MOVERS_MONITOR_ENABLED: bool = env_bool("MOVERS_MONITOR_ENABLED", bool(_MOVERS_MONITOR.get("enabled", False)))
MOVERS_MONITOR_POLL_INTERVAL_SEC: float = _as_float(_MOVERS_MONITOR.get("poll_interval_sec", 60), 60.0)
MOVERS_MONITOR_RVOL_FLOOR: float = _as_float(_MOVERS_MONITOR.get("rel_volume_floor", 1.0), 1.0)
MOVERS_MONITOR_MOMENTUM_FLIP: float = _as_float(_MOVERS_MONITOR.get("momentum_flip", -0.5), -0.5)
MOVERS_MONITOR_REQUIRE_VWAP_HOLD: bool = bool(_MOVERS_MONITOR.get("require_vwap_hold", True))
MOVERS_MONITOR_MIN_SCORE: float = _as_float(_MOVERS_MONITOR.get("min_score", 50), 50.0)
MOVERS_MONITOR_ALERT_ON_INVALIDATION: bool = env_bool(
    "MOVERS_MONITOR_ALERT_ON_INVALIDATION",
    bool(_MOVERS_MONITOR.get("alert_on_invalidation", True)))
# Phase 5 — always-on worker.
_MOVERS_WORKER: dict = _MOVERS.get("worker", {}) if isinstance(_MOVERS, dict) else {}
MOVERS_WORKER_POLL_INTERVAL_SEC: float = _as_float(_MOVERS_WORKER.get("poll_interval_sec", 60), 60.0)
MOVERS_WORKER_REDISCOVER_EVERY_CYCLES: int = _as_int(_MOVERS_WORKER.get("rediscover_every_cycles", 5), 5)
MOVERS_WORKER_MARKET_HOURS_ONLY: bool = bool(_MOVERS_WORKER.get("market_hours_only", True))
# Phase 7 — shared worker↔web state. JSON snapshot the worker publishes and the
# dashboard reads (/api/movers-worker). Path is shared across processes, like
# CIRCUIT_STATE_PATH. `stale_sec`: how old the snapshot may be before the UI
# calls the worker stale/offline (default ~3× the poll interval). `max_recent`:
# ring size for the recent-alerts / recent-invalidations history.
MOVERS_WORKER_STATE_PATH: str = _resolve_root(env(
    "MOVERS_WORKER_STATE_PATH",
    _MOVERS_WORKER.get("state_path") or str(_ROOT / "data" / "movers_worker.json"),
))
MOVERS_WORKER_STATE_STALE_SEC: float = env_float(
    "MOVERS_WORKER_STATE_STALE_SEC",
    _as_float(_MOVERS_WORKER.get("state_stale_sec", 180), 180.0),
)
MOVERS_WORKER_STATE_MAX_RECENT: int = env_int(
    "MOVERS_WORKER_STATE_MAX_RECENT",
    _as_int(_MOVERS_WORKER.get("state_max_recent", 20), 20),
)

# Phase 8 — real-time price streaming (Alpaca trade websocket). Opt-in and
# fail-open: default off, and the worker keeps polling if it can't start. Needs
# ALPACA_API_KEY/SECRET. `feed`: iex (free) or sip (paid). `stale_sec`: a tick
# older than this is treated as a quiet tape. `max_symbols`: subscription cap.
_MOVERS_STREAM: dict = _MOVERS.get("stream", {}) if isinstance(_MOVERS, dict) else {}
MOVERS_STREAM_ENABLED: bool = env_bool(
    "MOVERS_STREAM_ENABLED", bool(_MOVERS_STREAM.get("enabled", False)))
MOVERS_STREAM_FEED: str = env("MOVERS_STREAM_FEED", str(_MOVERS_STREAM.get("feed", "iex")))
MOVERS_STREAM_STALE_SEC: float = env_float(
    "MOVERS_STREAM_STALE_SEC", _as_float(_MOVERS_STREAM.get("stale_sec", 10), 10.0))
MOVERS_STREAM_MAX_SYMBOLS: int = env_int(
    "MOVERS_STREAM_MAX_SYMBOLS", _as_int(_MOVERS_STREAM.get("max_symbols", 30), 30))

# Run the always-on movers worker inside the web process (a daemon thread) so
# the dashboard and worker share one filesystem and the Phase 7 worker strip +
# Phase 8 live prices populate. Opt-in (env-only), default off — see
# src/scanner/worker_launcher.py and render.yaml.
RUN_WORKER_IN_PROCESS: bool = env_bool("RUN_WORKER_IN_PROCESS", False)

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

# Intraday data layer (upgrade plan P1.1). Intraday OHLCV bars (1m/5m/…) refresh
# far faster than the daily default TTL, and must be guarded against look-ahead
# (future-labelled bars) and stale frames. These knobs tune the intraday-aware
# cache TTL and the look-ahead/staleness assertions.
_INTRADAY: dict = get("intraday", {})
INTRADAY_DEFAULT_INTERVAL: str = env("INTRADAY_DEFAULT_INTERVAL", _INTRADAY.get("default_interval", "5m"))
# Boundary-align the intraday cache TTL: expire just AFTER the next bar boundary
# (+ a settle grace) so a newly-closed bar is picked up promptly, rather than a
# fixed TTL measured from fetch time. Default on.
INTRADAY_CACHE_BOUNDARY_ALIGN: bool = env_bool(
    "INTRADAY_CACHE_BOUNDARY_ALIGN", bool(_INTRADAY.get("cache_boundary_align", True))
)
# Grace after a bar boundary for the provider to publish the freshly-closed bar
# before the cache refetches (boundary-align path only).
INTRADAY_CACHE_SETTLE_SEC: float = env_float(
    "INTRADAY_CACHE_SETTLE_SEC", _as_float(_INTRADAY.get("cache_settle_sec", 5), 5.0)
)
# Floor on the derived intraday cache TTL (seconds), used only on the legacy
# (non-boundary-aligned) path. The TTL there defaults to the bar's own duration;
# this stops sub-floor churn on very short bars.
INTRADAY_CACHE_TTL_FLOOR_SEC: int = env_int(
    "INTRADAY_CACHE_TTL_FLOOR_SEC", _as_int(_INTRADAY.get("cache_ttl_floor_sec", 30), 30)
)
# A frame whose newest bar is older than this many bar-intervals is "stale".
INTRADAY_MAX_STALENESS_INTERVALS: float = env_float(
    "INTRADAY_MAX_STALENESS_INTERVALS", float(_INTRADAY.get("max_staleness_intervals", 3))
)
# Clock-skew tolerance before a future-labelled bar is dropped as look-ahead.
INTRADAY_FUTURE_BAR_TOLERANCE_SEC: float = env_float(
    "INTRADAY_FUTURE_BAR_TOLERANCE_SEC", float(_INTRADAY.get("future_bar_tolerance_sec", 5))
)

# Continuous intraday scan loop (upgrade plan P1.2).
_CONTINUOUS_SCAN: dict = get("continuous_scan", {})
CONTINUOUS_SCAN_INTERVAL_SEC: int = env_int(
    "CONTINUOUS_SCAN_INTERVAL_SEC", _as_int(_CONTINUOUS_SCAN.get("interval_sec", 45), 45)
)
CONTINUOUS_SCAN_INTERVAL: str = env("CONTINUOUS_SCAN_INTERVAL", _CONTINUOUS_SCAN.get("interval", "5m"))
CONTINUOUS_SCAN_UNIVERSE_CAP: int = env_int(
    "CONTINUOUS_SCAN_UNIVERSE_CAP", _as_int(_CONTINUOUS_SCAN.get("universe_cap", 100), 100)
)
CONTINUOUS_SCAN_DEDUP_TTL_SEC: int = env_int(
    "CONTINUOUS_SCAN_DEDUP_TTL_SEC", _as_int(_CONTINUOUS_SCAN.get("dedup_ttl_sec", 300), 300)
)
CONTINUOUS_SCAN_QUEUE_MAX: int = env_int(
    "CONTINUOUS_SCAN_QUEUE_MAX", _as_int(_CONTINUOUS_SCAN.get("queue_max", 500), 500)
)
CONTINUOUS_SCAN_REL_VOLUME_THRESHOLD: float = env_float(
    "CONTINUOUS_SCAN_REL_VOLUME_THRESHOLD", float(_CONTINUOUS_SCAN.get("rel_volume_threshold", 2.0))
)
CONTINUOUS_SCAN_MOMENTUM_PCT_THRESHOLD: float = env_float(
    "CONTINUOUS_SCAN_MOMENTUM_PCT_THRESHOLD", float(_CONTINUOUS_SCAN.get("momentum_pct_threshold", 1.5))
)
CONTINUOUS_SCAN_MOMENTUM_LOOKBACK_BARS: int = env_int(
    "CONTINUOUS_SCAN_MOMENTUM_LOOKBACK_BARS", _as_int(_CONTINUOUS_SCAN.get("momentum_lookback_bars", 6), 6)
)
CONTINUOUS_SCAN_BREAKOUT_LOOKBACK_BARS: int = env_int(
    "CONTINUOUS_SCAN_BREAKOUT_LOOKBACK_BARS", _as_int(_CONTINUOUS_SCAN.get("breakout_lookback_bars", 20), 20)
)
CONTINUOUS_SCAN_MIN_BARS: int = env_int(
    "CONTINUOUS_SCAN_MIN_BARS", _as_int(_CONTINUOUS_SCAN.get("min_bars", 10), 10)
)
CONTINUOUS_SCAN_PREMARKET_START_ET: str = env(
    "CONTINUOUS_SCAN_PREMARKET_START_ET", _CONTINUOUS_SCAN.get("premarket_start_et", "07:00")
)
CONTINUOUS_SCAN_MIN_GAP_PCT: float = env_float(
    "CONTINUOUS_SCAN_MIN_GAP_PCT", float(_CONTINUOUS_SCAN.get("min_gap_pct", 5.0))
)

# Intraday signal + entry/exit logic (upgrade plan P1.3).
_INTRADAY_SIGNAL: dict = get("intraday_signal", {})
INTRADAY_OPENING_RANGE_MINUTES: int = env_int(
    "INTRADAY_OPENING_RANGE_MINUTES", _as_int(_INTRADAY_SIGNAL.get("opening_range_minutes", 30), 30)
)
INTRADAY_MIN_REL_VOLUME: float = env_float(
    "INTRADAY_MIN_REL_VOLUME", float(_INTRADAY_SIGNAL.get("min_rel_volume", 1.5))
)
INTRADAY_REQUIRE_ABOVE_VWAP: bool = env_bool(
    "INTRADAY_REQUIRE_ABOVE_VWAP", bool(_INTRADAY_SIGNAL.get("require_above_vwap", True))
)
INTRADAY_ATR_PERIOD: int = env_int(
    "INTRADAY_ATR_PERIOD", _as_int(_INTRADAY_SIGNAL.get("atr_period", 14), 14)
)
INTRADAY_MIN_SESSION_BARS: int = env_int(
    "INTRADAY_MIN_SESSION_BARS", _as_int(_INTRADAY_SIGNAL.get("min_session_bars", 6), 6)
)
INTRADAY_CONSUMER_MAX_PER_CYCLE: int = env_int(
    "INTRADAY_CONSUMER_MAX_PER_CYCLE", _as_int(_INTRADAY_SIGNAL.get("consumer_max_per_cycle", 20), 20)
)

# Sweep-reclaim reversal entry (liquidity-grab timing). Deterministic intraday
# reversal: sweep of a recent swing low + reclaim close on volume, stop below the
# swept wick. Default-OFF (SWEEP_RECLAIM_ENABLED) — an opt-in, validate-first
# timing hypothesis the consumer only evaluates when explicitly enabled.
_SWEEP: dict = get("sweep_reclaim", {})
SWEEP_RECLAIM_ENABLED: bool = env_bool(
    "SWEEP_RECLAIM_ENABLED", bool(_SWEEP.get("enabled", False))
)
SWEEP_LOOKBACK_BARS: int = env_int(
    "SWEEP_LOOKBACK_BARS", _as_int(_SWEEP.get("lookback_bars", 15), 15)
)
SWEEP_RECLAIM_WINDOW_BARS: int = env_int(
    "SWEEP_RECLAIM_WINDOW_BARS", _as_int(_SWEEP.get("reclaim_window_bars", 2), 2)
)
SWEEP_MIN_REL_VOLUME: float = env_float(
    "SWEEP_MIN_REL_VOLUME", float(_SWEEP.get("min_rel_volume", 1.5))
)
SWEEP_REQUIRE_VOLUME: bool = env_bool(
    "SWEEP_REQUIRE_VOLUME", bool(_SWEEP.get("require_volume", False))
)
SWEEP_STOP_BUFFER_PCT: float = env_float(
    "SWEEP_STOP_BUFFER_PCT", float(_SWEEP.get("stop_buffer_pct", 0.001))
)
SWEEP_MAX_RECLAIM_EXTENSION_PCT: float = env_float(
    "SWEEP_MAX_RECLAIM_EXTENSION_PCT", float(_SWEEP.get("max_reclaim_extension_pct", 0.05))
)
SWEEP_MIN_SESSION_BARS: int = env_int(
    "SWEEP_MIN_SESSION_BARS", _as_int(_SWEEP.get("min_session_bars", 20), 20)
)

# Intraday backtest harness (--mode intraday-backtest).
_INTRADAY_BT: dict = get("intraday_backtest", {})
INTRADAY_BACKTEST_MAX_LOOKBACK_BARS: int = env_int(
    "INTRADAY_BACKTEST_MAX_LOOKBACK_BARS", _as_int(_INTRADAY_BT.get("max_lookback_bars", 400), 400)
)
INTRADAY_BACKTEST_MIN_TRADES: int = env_int(
    "INTRADAY_BACKTEST_MIN_TRADES", _as_int(_INTRADAY_BT.get("min_trades", 30), 30)
)
INTRADAY_BACKTEST_MIN_PROFIT_FACTOR: float = env_float(
    "INTRADAY_BACKTEST_MIN_PROFIT_FACTOR", _as_float(_INTRADAY_BT.get("min_profit_factor", 1.2), 1.2)
)
INTRADAY_BACKTEST_MAX_DRAWDOWN_PCT: float = env_float(
    "INTRADAY_BACKTEST_MAX_DRAWDOWN_PCT", _as_float(_INTRADAY_BT.get("max_drawdown_pct", -25.0), -25.0)
)
INTRADAY_BACKTEST_MAX_TICKERS: int = env_int(
    "INTRADAY_BACKTEST_MAX_TICKERS", _as_int(_INTRADAY_BT.get("max_tickers", 50), 50)
)
INTRADAY_BACKTEST_MIN_FRAME_BARS: int = env_int(
    "INTRADAY_BACKTEST_MIN_FRAME_BARS", _as_int(_INTRADAY_BT.get("min_frame_bars", 5), 5)
)

# Catalyst gating (upgrade plan P1.4): calendar-backed earnings + catalyst-tag veto.
_CATALYSTS: dict = get("catalysts", {})
_ecal_path = env(
    "CATALYST_EARNINGS_CALENDAR_PATH",
    _CATALYSTS.get("earnings_calendar_path") or str(_ROOT / "data" / "earnings_calendar.json"),
)
# Resolve a relative configured path against the project root, so a service and a
# manually-run `--mode earnings-calendar` refresh don't read/write different files
# when launched from different working directories.
CATALYST_EARNINGS_CALENDAR_PATH: str = _resolve_root(_ecal_path)
CATALYST_ENABLE_GATE: bool = env_bool(
    "CATALYST_ENABLE_GATE", bool(_CATALYSTS.get("enable_catalyst_gate", True))
)
_DEFAULT_VETO_TAGS = ["TRADING_HALT", "SEC_INVESTIGATION", "FRAUD", "GOING_CONCERN", "BANKRUPTCY"]
CATALYST_VETO_TAGS: list = [
    str(t).upper() for t in (_CATALYSTS.get("veto_tags") or _DEFAULT_VETO_TAGS)
]

# Execution-quality measurement (upgrade plan P2.1).
_EXEC_QUALITY: dict = get("execution_quality", {})
EXEC_QUALITY_LEDGER_PATH: str = _resolve_root(env(
    "EXEC_QUALITY_LEDGER_PATH",
    _EXEC_QUALITY.get("ledger_path") or str(_ROOT / "data" / "execution_quality.json"),
))
EXEC_QUALITY_MAX_FILLS: int = env_int(
    "EXEC_QUALITY_MAX_FILLS", _as_int(_EXEC_QUALITY.get("max_fills_retained", 5000), 5000)
)
EXEC_QUALITY_USE_MEASURED_SLIPPAGE: bool = env_bool(
    "EXEC_QUALITY_USE_MEASURED_SLIPPAGE", bool(_EXEC_QUALITY.get("use_measured_slippage", False))
)
EXEC_QUALITY_MIN_FILLS_FOR_MEASURED: int = env_int(
    "EXEC_QUALITY_MIN_FILLS_FOR_MEASURED", _as_int(_EXEC_QUALITY.get("min_fills_for_measured", 20), 20)
)

# Smart order handling (upgrade plan P2.2).
_SMART_ORDERS: dict = get("smart_orders", {})
SMART_ORDERS_ENABLED: bool = env_bool(
    "SMART_ORDERS_ENABLED", bool(_SMART_ORDERS.get("enabled", False))
)
SMART_ORDERS_ENTRY_OFFSET_FLOOR_PCT: float = env_float(
    "SMART_ORDERS_ENTRY_OFFSET_FLOOR_PCT", float(_SMART_ORDERS.get("entry_offset_floor_pct", 0.001))
)
SMART_ORDERS_ENTRY_OFFSET_CAP_PCT: float = env_float(
    "SMART_ORDERS_ENTRY_OFFSET_CAP_PCT", float(_SMART_ORDERS.get("entry_offset_cap_pct", 0.01))
)

# Observability stack (upgrade plan P2.3): scan-metrics ledger + operational alerts.
_OBSERVABILITY: dict = get("observability", {})
METRICS_LEDGER_PATH: str = _resolve_root(env(
    "METRICS_LEDGER_PATH",
    _OBSERVABILITY.get("metrics_ledger_path") or str(_ROOT / "data" / "metrics.json"),
))
METRICS_MAX_SCANS: int = env_int(
    "METRICS_MAX_SCANS", _as_int(_OBSERVABILITY.get("metrics_max_scans", 2000), 2000)
)
# Seconds without a scan before the dashboard flags a "data gap" and (if enabled)
# an alert fires. Default 0 = DISABLED: raw elapsed time can't distinguish a real
# outage from a normal quiet period (overnight/weekends), so gap detection is
# opt-in — set it above the deployment's real inter-scan cadence before enabling.
METRICS_DATA_GAP_SECONDS: float = env_float(
    "METRICS_DATA_GAP_SECONDS", float(_OBSERVABILITY.get("data_gap_seconds", 0.0))
)
# Operational alerting (breaker trips, data gaps) — default OFF, opt-in.
OBSERVABILITY_ALERTS_ENABLED: bool = env_bool(
    "OBSERVABILITY_ALERTS_ENABLED",
    bool((_OBSERVABILITY.get("alerts") or {}).get("enabled", False)),
)

# Explainability layer (upgrade plan P3.1): optional LLM bull/bear narrative.
# Advisory only — never gates or places an order. Default OFF and cost-capped.
_EXPLAIN: dict = get("explain", {})
EXPLAIN_ENABLED: bool = env_bool("EXPLAIN_ENABLED", bool(_EXPLAIN.get("enabled", False)))
EXPLAIN_MODEL: str = env("EXPLAIN_MODEL", str(_EXPLAIN.get("model", "claude-opus-5")))
EXPLAIN_MAX_TOKENS: int = env_int(
    "EXPLAIN_MAX_TOKENS", _as_int(_EXPLAIN.get("max_tokens", 1000), 1000)
)
EXPLAIN_TIMEOUT_SECONDS: float = env_float(
    "EXPLAIN_TIMEOUT_SECONDS", float(_EXPLAIN.get("timeout_seconds", 30.0))
)
# Per-day call cap (cost guard); <= 0 disables the limit.
EXPLAIN_DAILY_CALL_CAP: int = env_int(
    "EXPLAIN_DAILY_CALL_CAP", _as_int(_EXPLAIN.get("daily_call_cap", 50), 50)
)
EXPLAIN_CALL_LEDGER_PATH: str = _resolve_root(env(
    "EXPLAIN_CALL_LEDGER_PATH",
    _EXPLAIN.get("call_ledger_path") or str(_ROOT / "data" / "explain_calls.json"),
))
# LLM provider for the explain layer. "anthropic" (default) uses the Anthropic
# Messages API + ANTHROPIC_API_KEY. Any other value uses an OpenAI-compatible
# Chat Completions endpoint (OpenAI, NVIDIA Nemotron, OpenRouter, a local vLLM
# server, …): set EXPLAIN_BASE_URL + EXPLAIN_API_KEY and an EXPLAIN_MODEL id from
# that provider's catalog. This is the only switch needed to run e.g. Nemotron
# instead of Claude.
EXPLAIN_PROVIDER: str = env("EXPLAIN_PROVIDER", str(_EXPLAIN.get("provider", "anthropic")))
EXPLAIN_BASE_URL: str = env("EXPLAIN_BASE_URL", str(_EXPLAIN.get("base_url", "")))
EXPLAIN_API_KEY: str = env("EXPLAIN_API_KEY", str(_EXPLAIN.get("api_key", "")))
EXPLAIN_TEMPERATURE: float = env_float(
    "EXPLAIN_TEMPERATURE", float(_EXPLAIN.get("temperature", 0.3))
)

# Portfolio + Risk Engine (universal-engine roadmap Phase 4). ADVISORY and
# default-OFF: volatility/correlation-aware sizing + portfolio-level risk metrics
# (leverage, single-name weight, portfolio heat, correlated-cluster exposure,
# drawdown). Never replaces RiskGuard; gate_entries is a separate opt-in that
# lets the engine veto/scale in the money path.
_PORTFOLIO: dict = get("portfolio", {})
PORTFOLIO_ENABLED: bool = env_bool(
    "PORTFOLIO_ENABLED", bool(_PORTFOLIO.get("enabled", False))
)
PORTFOLIO_GATE_ENTRIES: bool = env_bool(
    "PORTFOLIO_GATE_ENTRIES", bool(_PORTFOLIO.get("gate_entries", False))
)
PORTFOLIO_MIN_RETURN_HISTORY: int = env_int(
    "PORTFOLIO_MIN_RETURN_HISTORY", _as_int(_PORTFOLIO.get("min_return_history", 10), 10)
)
PORTFOLIO_MAX_GROSS_LEVERAGE: float = env_float(
    "PORTFOLIO_MAX_GROSS_LEVERAGE", _as_float(_PORTFOLIO.get("max_gross_leverage"), 1.0)
)
PORTFOLIO_MAX_SINGLE_WEIGHT_PCT: float = env_float(
    "PORTFOLIO_MAX_SINGLE_WEIGHT_PCT", _as_float(_PORTFOLIO.get("max_single_weight_pct"), 20.0)
)
PORTFOLIO_MAX_HEAT_PCT: float = env_float(
    "PORTFOLIO_MAX_HEAT_PCT", _as_float(_PORTFOLIO.get("max_portfolio_heat_pct"), 6.0)
)
PORTFOLIO_CORRELATION_THRESHOLD: float = env_float(
    "PORTFOLIO_CORRELATION_THRESHOLD", _as_float(_PORTFOLIO.get("correlation_threshold"), 0.6)
)
PORTFOLIO_MAX_CLUSTER_WEIGHT_PCT: float = env_float(
    "PORTFOLIO_MAX_CLUSTER_WEIGHT_PCT", _as_float(_PORTFOLIO.get("max_cluster_weight_pct"), 35.0)
)
PORTFOLIO_MAX_DRAWDOWN_HALT_PCT: float = env_float(
    "PORTFOLIO_MAX_DRAWDOWN_HALT_PCT", _as_float(_PORTFOLIO.get("max_drawdown_halt_pct"), 15.0)
)
PORTFOLIO_PEAK_EQUITY_PATH: str = _resolve_root(env(
    "PORTFOLIO_PEAK_EQUITY_PATH",
    _PORTFOLIO.get("peak_equity_path") or str(_ROOT / "data" / "portfolio_peak_equity.json"),
))

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

# Circuit breakers (upgrade plan P0.3). Halt NEW entries when market data is
# stale, when the broker has failed repeatedly, or when an operator flips the
# global halt switch. These sit in RiskGuard alongside the loss kill-switches
# and are surfaced/toggled from the dashboard. Enforced on the money path only;
# they never touch exits (a halt must not strand an open position without its
# stop). Env overrides win over config.yaml so an operator can retune fast.
_CIRCUIT: dict = get("circuit_breakers", {})
# Block new entries when a plan's market view (its `quote_as_of` capture time)
# is older than this many seconds at execution. 0/negative disables the
# staleness breaker; plans with no timestamp fail OPEN (the gate can only fire
# on a known age). Default 900s (15 min) is a coarse daily-bar guard: it must
# comfortably exceed a full-universe scan (60-120s) so normal scan→execute flow
# never trips, while still catching a stuck/queued/overnight plan. P1 (intraday
# bars) will tighten this to a seconds-scale real-time freshness check.
CIRCUIT_DATA_STALENESS_MAX_SECONDS: float = env_float(
    "CIRCUIT_DATA_STALENESS_MAX_SECONDS",
    float(_CIRCUIT.get("data_staleness_max_seconds", 900)),
)
# Trip the broker breaker after this many broker failures within the rolling
# window below. <=0 disables it.
CIRCUIT_BROKER_ERROR_THRESHOLD: int = env_int(
    "CIRCUIT_BROKER_ERROR_THRESHOLD",
    _as_int(_CIRCUIT.get("broker_error_threshold", 3), 3),
)
CIRCUIT_BROKER_ERROR_WINDOW_SECONDS: float = env_float(
    "CIRCUIT_BROKER_ERROR_WINDOW_SECONDS",
    float(_CIRCUIT.get("broker_error_window_seconds", 300)),
)
# JSON state file (manual halt flag + recent broker-error timestamps). Shared
# across processes — the dashboard writes the halt flag, the executor reads it.
CIRCUIT_STATE_PATH: str = _resolve_root(env(
    "CIRCUIT_STATE_PATH",
    _CIRCUIT.get("state_path") or str(_ROOT / "data" / "circuit_breaker.json"),
))

# Paper-trading scorecard gates (upgrade plan P0.4). The scorecard computes the
# same trade-level metrics as the backtest from realized paper trades; when the
# performance gate is on, going live requires clearing these floors (in addition
# to paper_trade_days_required tenure).
_PAPER_SCORECARD: dict = get("paper_scorecard", {})
PAPER_SCORECARD_MIN_TRADES: int = int(_PAPER_SCORECARD.get("min_trades", 20))
PAPER_SCORECARD_PROFIT_FACTOR_FLOOR: float = float(
    _PAPER_SCORECARD.get("profit_factor_floor", 1.2)
)
PAPER_SCORECARD_PROB_PROFITABLE_FLOOR: float = float(
    _PAPER_SCORECARD.get("prob_profitable_floor", 0.55)
)
PAPER_SCORECARD_REQUIRE_POSITIVE_EXPECTANCY: bool = bool(
    _PAPER_SCORECARD.get("require_positive_expectancy", True)
)
PAPER_SCORECARD_PERFORMANCE_GATE_ENABLED: bool = bool(
    _PAPER_SCORECARD.get("performance_gate_enabled", True)
)
PAPER_SCORECARD_MAX_TRADES: int = int(_PAPER_SCORECARD.get("max_trades_retained", 5000))

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

# Screener presets — named pre-trade filters (config.yaml `screener.presets`).
# A dict of {preset_name: {criteria...}} consumed by src/screener/. Empty when
# unconfigured; unknown preset names are rejected loudly at call time.
SCREENER_PRESETS: dict = get("screener.presets", {}) or {}
# Feature lookback periods (config-driven, not hardcoded). SMA periods drive
# which `sma{p}` features exist, so a preset can reference any configured period.
SCREENER_SMA_PERIODS: list = [
    _as_int(p, 0) for p in (get("screener.sma_periods", [20, 50, 200]) or []) if _as_int(p, 0) > 0
] or [20, 50, 200]
SCREENER_RSI_PERIOD: int = env_int(
    "SCREENER_RSI_PERIOD", _as_int(get("screener.rsi_period", 14), 14)
)
SCREENER_VOLUME_LOOKBACK: int = env_int(
    "SCREENER_VOLUME_LOOKBACK", _as_int(get("screener.volume_lookback", 20), 20)
)
SCREENER_HIGH_LOOKBACK: int = env_int(
    "SCREENER_HIGH_LOOKBACK", _as_int(get("screener.high_lookback", 50), 50)
)

# Penny stock mode — an OPT-IN low-price/small-cap profile. Default OFF. When
# enabled it does NOT remove protection: it swaps the standard disqualifiers for
# penny-specific HARD RAILS (below). Scan/paper only until validated — the money
# path still needs --execute/--live-execute + ALPACA_PAPER handling.
_PENNY: dict = get("penny", {})
ALLOW_PENNY_STOCKS: bool = env_bool("ALLOW_PENNY_STOCKS", bool(_PENNY.get("enabled", False)))
SIGNAL_MIN_PRICE: float = env_float("SIGNAL_MIN_PRICE", 1.0)
CUSTOM_WATCHLIST: str = env("CUSTOM_WATCHLIST", str(_PENNY.get("custom_watchlist", "")))

# Penny hard rails (used by src/signals/filters.py when ALLOW_PENNY_STOCKS is on).
# Protective defaults — loosen at your own risk; the dollar-volume and market-cap
# floors are the ones that actually keep out manipulated nano-caps.
PENNY_MIN_PRICE: float = env_float("PENNY_MIN_PRICE", _as_float(_PENNY.get("min_price"), 0.50))
PENNY_MAX_PRICE: float = env_float("PENNY_MAX_PRICE", _as_float(_PENNY.get("max_price"), 10.0))
PENNY_MIN_SHARE_VOLUME: int = env_int(
    "PENNY_MIN_SHARE_VOLUME", _as_int(_PENNY.get("min_share_volume", 100_000), 100_000)
)
# price x avg 20-day volume — the primary liquidity gate (catches both huge-share
# sub-penny names and high-price zero-volume names that a share count misses).
PENNY_MIN_DOLLAR_VOLUME: float = env_float(
    "PENNY_MIN_DOLLAR_VOLUME", _as_float(_PENNY.get("min_dollar_volume"), 3_000_000.0)
)
# A FLOOR, not 0: sub-$50M caps are the pump-and-dump zone.
PENNY_MIN_MARKET_CAP: float = env_float(
    "PENNY_MIN_MARKET_CAP", _as_float(_PENNY.get("min_market_cap"), 50_000_000.0)
)
PENNY_MAX_5DAY_GAIN_PCT: float = env_float(
    "PENNY_MAX_5DAY_GAIN_PCT", _as_float(_PENNY.get("max_5day_gain_pct"), 30.0)
)

# Provider concurrency
PROVIDER_CONCURRENCY_LIMIT: int = env_int("PROVIDER_CONCURRENCY_LIMIT", 10)
