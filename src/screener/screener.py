"""Screener preset engine.

`evaluate_preset(preset, features)` checks a ticker's features against a named
preset's criteria and returns a `ScreenResult`. `screen_ticker` / `screen_universe`
fetch the data and run it over one or many tickers.

Criterion semantics:
  * A **technical** criterion whose feature can't be computed (not enough
    history) → the ticker FAILS that criterion (can't confirm → don't include).
  * A **fundamental** criterion (`min_roe_pct`, `max_debt_equity`) whose value is
    unavailable → the criterion is SKIPPED (the free data provider is unreliable
    for these, so a missing value must not silently reject every candidate).

A screener is a *filter*, not a trade signal. Output feeds the scan / intraday
entry logic; nothing here sizes or places an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from src.screener.features import compute_screen_features
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

_FUNDAMENTAL_KEYS = {"min_roe_pct", "max_debt_equity"}


@dataclass
class ScreenResult:
    ticker: str
    passed: bool
    reasons: list[str] = field(default_factory=list)  # failed criteria (empty = passed)
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "features": dict(self.features),
        }


def get_preset(name: str) -> Optional[dict]:
    """Resolve a preset by name from config (`screener.presets`). None if absent."""
    presets = config.SCREENER_PRESETS or {}
    preset = presets.get(name)
    return dict(preset) if isinstance(preset, dict) else None


def evaluate_preset(preset: dict, feat: dict) -> ScreenResult:
    """Return a ScreenResult for `feat` against `preset`. Never raises."""
    ticker = feat.get("ticker") or "?"
    reasons: list[str] = []

    def fail(msg: str) -> None:
        reasons.append(msg)

    price = feat.get("price")

    # ── price band ───────────────────────────────────────────────────────────
    if "min_price" in preset:
        if price is None:
            fail("INSUFFICIENT_DATA: price")
        elif price < float(preset["min_price"]):
            fail(f"PRICE_BELOW_MIN: ${price:.2f} < ${float(preset['min_price']):.2f}")
    if "max_price" in preset:
        if price is None:
            fail("INSUFFICIENT_DATA: price")
        elif price > float(preset["max_price"]):
            fail(f"PRICE_ABOVE_MAX: ${price:.2f} > ${float(preset['max_price']):.2f}")

    # ── RSI band ─────────────────────────────────────────────────────────────
    rsi = feat.get("rsi14")
    if "rsi_max" in preset:
        if rsi is None:
            fail("INSUFFICIENT_DATA: rsi14")
        elif rsi > float(preset["rsi_max"]):
            fail(f"RSI_ABOVE_MAX: {rsi:.1f} > {float(preset['rsi_max']):.1f}")
    if "rsi_min" in preset:
        if rsi is None:
            fail("INSUFFICIENT_DATA: rsi14")
        elif rsi < float(preset["rsi_min"]):
            fail(f"RSI_BELOW_MIN: {rsi:.1f} < {float(preset['rsi_min']):.1f}")

    # ── moving-average relationships ─────────────────────────────────────────
    for period in preset.get("require_above_sma", []) or []:
        sma = feat.get(f"sma{period}")
        if sma is None or price is None:
            fail(f"INSUFFICIENT_DATA: sma{period}")
        elif not price > sma:
            fail(f"NOT_ABOVE_SMA{period}: ${price:.2f} <= ${sma:.2f}")
    for period in preset.get("require_below_sma", []) or []:
        sma = feat.get(f"sma{period}")
        if sma is None or price is None:
            fail(f"INSUFFICIENT_DATA: sma{period}")
        elif not price < sma:
            fail(f"NOT_BELOW_SMA{period}: ${price:.2f} >= ${sma:.2f}")

    # ── new 50-day high ──────────────────────────────────────────────────────
    if preset.get("require_new_high_50"):
        is_high = feat.get("is_new_high_50")
        if is_high is None:
            fail("INSUFFICIENT_DATA: high_50")
        elif not is_high:
            fail("NOT_NEW_50D_HIGH")

    # ── volume ───────────────────────────────────────────────────────────────
    if "min_avg_volume" in preset:
        avg = feat.get("avg_volume_20")
        if avg is None:
            fail("INSUFFICIENT_DATA: avg_volume_20")
        elif avg < float(preset["min_avg_volume"]):
            fail(f"AVG_VOLUME_LOW: {avg:,.0f} < {float(preset['min_avg_volume']):,.0f}")
    if "min_rel_volume" in preset:
        rv = feat.get("rel_volume")
        if rv is None:
            fail("INSUFFICIENT_DATA: rel_volume")
        elif rv < float(preset["min_rel_volume"]):
            fail(f"REL_VOLUME_LOW: {rv:.2f}x < {float(preset['min_rel_volume']):.2f}x")

    # ── recent direction ─────────────────────────────────────────────────────
    if preset.get("require_change_up"):
        chg = feat.get("pct_change_1d")
        if chg is None:
            fail("INSUFFICIENT_DATA: pct_change_1d")
        elif not chg > 0:
            fail(f"NOT_TURNING_UP: 1d change {chg:.2f}% <= 0")

    # ── optional fundamentals (SKIP when the value is unavailable) ────────────
    if "min_roe_pct" in preset:
        roe = feat.get("roe_pct")
        if roe is not None and roe < float(preset["min_roe_pct"]):
            fail(f"ROE_LOW: {roe:.1f}% < {float(preset['min_roe_pct']):.1f}%")
    if "max_debt_equity" in preset:
        de = feat.get("debt_equity")
        if de is not None and de > float(preset["max_debt_equity"]):
            fail(f"DEBT_EQUITY_HIGH: {de:.2f} > {float(preset['max_debt_equity']):.2f}")

    return ScreenResult(ticker=ticker, passed=not reasons, reasons=reasons, features=feat)


def screen_ticker(
    preset: dict,
    ticker: str,
    context_builder: Optional[Callable[[str], Any]] = None,
) -> ScreenResult:
    """Fetch one ticker's data and evaluate it against `preset`. Never raises."""
    if context_builder is None:
        from src.signals.context import build_signal_context
        context_builder = build_signal_context
    try:
        ctx = context_builder(ticker)
    except Exception as exc:
        log.debug(f"{ticker}: screen context error: {exc}")
        ctx = None
    if ctx is None:
        return ScreenResult(
            ticker=ticker.upper(), passed=False,
            reasons=["NO_DATA: could not build context"], features={"ticker": ticker.upper()},
        )
    return evaluate_preset(preset, compute_screen_features(ctx))


def screen_universe(
    preset_name: str,
    tickers: Sequence[str],
    context_builder: Optional[Callable[[str], Any]] = None,
) -> list[ScreenResult]:
    """Run a named preset over `tickers`; return the PASSING results only.

    An unknown preset name raises ValueError (a caller typo should be loud);
    per-ticker failures are isolated and never abort the sweep.
    """
    preset = get_preset(preset_name)
    if preset is None:
        raise ValueError(
            f"Unknown screener preset '{preset_name}'. "
            f"Available: {', '.join(sorted((config.SCREENER_PRESETS or {}).keys())) or '(none)'}"
        )
    passed: list[ScreenResult] = []
    for t in tickers:
        res = screen_ticker(preset, t, context_builder=context_builder)
        if res.passed:
            passed.append(res)
    log.info(
        f"Screener '{preset_name}': {len(passed)}/{len(tickers)} tickers passed"
    )
    return passed
