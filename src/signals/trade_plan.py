"""
Trade plan generator — entry, stop loss, targets, and position sizing.

Position Sizing (1% Risk Rule):
    Risk Amount   = Account Size × Risk Percent (default 1%)
    Position Size = Risk Amount / (Entry - Stop Loss)
    Max Position  = min(Position Size, Account × Max_Pct)

Stop Loss:
    Entry - (ATR_14 × 2.0)

Targets (Fibonacci R:R extensions):
    T1 = Entry + 1.5 × risk  (exit 30%)
    T2 = Entry + 2.5 × risk  (exit 40%)
    T3 = Entry + 4.0 × risk  (let run 30%)

Expected Value (45% win rate, 2.5:1 avg R:R):
    EV = (0.45 × 2.5 × risk) - (0.55 × risk) = 0.575 × risk
"""

import math

import pandas as pd

from src.technical.support_resistance import calculate_support_resistance
from src.technical.volatility import calculate_volatility_indicators
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

WIN_RATE  = 0.45
AVG_RR    = 2.5

# The planner is LONG-ONLY: every plan it builds is a bullish entry with a stop
# below and targets above. Handing that to a SELL/HOLD signal invites buying
# into a downtrend (audit M3) — those signals get an explicit skip instead.
_LONG_SIGNALS = {"STRONG_BUY", "BUY", "WEAK_BUY"}


def generate_trade_plan(
    ticker: str,
    signal: dict,
    account_size: float | None = None,
    df: pd.DataFrame | None = None,
) -> dict:
    """
    Generate a complete trade plan from a signal dict.

    Args:
        ticker:       Stock ticker.
        signal:       Output from generate_signal().
        account_size: Portfolio size in USD (overrides config).
        df:           Pre-fetched OHLCV DataFrame (avoids redundant download).

    Returns a JSON-serialisable trade plan dict.
    """
    if account_size is None:
        account_size = config.ACCOUNT_SIZE

    signal_label = str(signal.get("signal", "UNKNOWN")).upper()
    if signal_label not in _LONG_SIGNALS:
        return {
            "ticker": ticker,
            "signal": signal_label,
            "skip": True,
            "reason": (
                f"Long-only planner: a {signal_label} signal gets no bullish "
                "entry/stop/target plan. Close or avoid the position instead."
            ),
            "disclaimer": "NOT FINANCIAL ADVICE. All trading involves risk of loss.",
        }

    # Use pre-fetched df or load from signal
    if df is None:
        df = signal.get("_df")
    if df is None or df.empty:
        from src.data.fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, period="6mo")

    if df is None or df.empty:
        return {"error": f"No price data for {ticker}"}

    current_price = signal.get("current_price") or float(df["Close"].iloc[-1])

    # ── Volatility ─────────────────────────────────────────────────────────
    vol_indicators = signal.get("volatility") or calculate_volatility_indicators(df)
    atr14 = None
    if vol_indicators:
        atr_series = vol_indicators.get("ATR_14")
        if atr_series is not None and hasattr(atr_series, "iloc") and len(atr_series) > 0:
            v = atr_series.iloc[-1]
            atr14 = float(v) if v == v else None  # NaN guard

    if atr14 is None or atr14 == 0:
        atr14 = current_price * 0.02  # Fallback: 2% of price

    # ── Support / Resistance ───────────────────────────────────────────────
    sr = signal.get("support_resistance") or calculate_support_resistance(df, current_price)

    # ── Entry ──────────────────────────────────────────────────────────────
    entry_price = round(current_price * 0.998, 2)  # Slight discount for limit order
    if entry_price <= 0:
        return {"error": f"Invalid entry price for {ticker} (<= 0)"}

    # ── Stop Loss ──────────────────────────────────────────────────────────
    multiplier = config.ATR_STOP_MULTIPLIER
    stop_loss  = round(entry_price - atr14 * multiplier, 2)
    stop_pct   = round((entry_price - stop_loss) / entry_price * 100, 2)

    # ── Targets ────────────────────────────────────────────────────────────
    risk = entry_price - stop_loss
    rr_ratios  = list(config.TARGET_RR_RATIOS)   # [1.5, 2.5, 4.0]
    exit_pcts  = list(config.TARGET_EXIT_PCTS)   # [30, 40, 30]

    # Fail loudly on inconsistent target config (audit M4). Exactly three
    # targets are supported END-TO-END: PositionTracker/executor allocate
    # fixed T1/T2/T3 tranches (missing targets would be fabricated at the
    # entry price) and the backtest models three exits. A loud config error
    # beats a silent zip() truncation or a breakeven "target".
    if len(rr_ratios) != 3 or len(exit_pcts) != 3:
        return {
            "error": (
                f"Config error: target_rr_ratios and target_exit_pcts must each "
                f"have exactly 3 entries (got {len(rr_ratios)} and "
                f"{len(exit_pcts)}); the execution tracker and backtest support "
                "exactly T1/T2/T3 today."
            )
        }
    # Values must be finite and sane before they drive target geometry: a
    # negative RR would place a "target" below entry, and a negative exit pct
    # can still sum to 100 (audit M4 hardening).
    if not all(isinstance(rr, (int, float)) and math.isfinite(rr) and rr > 0 for rr in rr_ratios):
        return {"error": f"Config error: target_rr_ratios must be finite and > 0 (got {rr_ratios})."}
    if not all(isinstance(ep, (int, float)) and math.isfinite(ep) and 0 <= ep <= 100 for ep in exit_pcts):
        return {"error": f"Config error: target_exit_pcts must each be within [0, 100] (got {exit_pcts})."}
    if abs(sum(exit_pcts) - 100.0) > 1e-6:
        return {
            "error": (
                f"Config error: target_exit_pcts must sum to 100 "
                f"(got {sum(exit_pcts)})."
            )
        }

    raw_targets = [round(entry_price + risk * rr, 2) for rr in rr_ratios]

    # Snap T1 to nearest resistance if it's between entry and T2
    nearest_res = sr.get("nearest_resistance")
    if nearest_res and len(raw_targets) >= 2 and entry_price < nearest_res < raw_targets[1]:
        raw_targets[0] = round(float(nearest_res) * 0.995, 2)

    targets = {}
    labels = [f"T{i + 1}" for i in range(len(rr_ratios))]
    for label, price, rr, ep in zip(labels, raw_targets, rr_ratios, exit_pcts, strict=True):
        targets[label] = {
            "price":    price,
            "exit_pct": ep,
            "rr":       f"{rr}:1",
        }

    # ── Position Sizing ────────────────────────────────────────────────────
    risk_pct    = config.RISK_PER_TRADE   # e.g. 0.01 = 1%
    max_pos_pct = config.MAX_POSITION_PCT # e.g. 0.10 = 10%

    risk_dollars     = account_size * risk_pct
    shares_by_risk   = risk_dollars / risk if risk > 0 else 0
    shares_by_max    = (account_size * max_pos_pct) / entry_price if entry_price > 0 else 0
    # Never floor to 1: a forced share on an expensive stock / small account
    # breaches max_position_pct (audit C3). Truncate to whole shares and flag
    # "too small to trade" instead of silently oversizing.
    shares           = int(min(shares_by_risk, shares_by_max))

    if shares < 1:
        return {
            "ticker": ticker,
            "signal": signal.get("signal", "UNKNOWN"),
            "skip": True,
            "reason": (
                f"Position too small to trade within risk limits "
                f"(entry ${entry_price:.2f}, max_position {max_pos_pct:.0%} of "
                f"${account_size:,.0f} → <1 share). Increase account size or "
                f"choose a lower-priced instrument."
            ),
            "disclaimer": "NOT FINANCIAL ADVICE. All trading involves risk of loss.",
        }

    position_value  = round(shares * entry_price, 2)
    actual_risk     = round(shares * risk, 2)
    actual_risk_pct = round(actual_risk / account_size * 100, 2)
    pos_pct_account = round(position_value / account_size * 100, 2)

    # ── Expected Value ─────────────────────────────────────────────────────
    ev = round((WIN_RATE * AVG_RR * actual_risk) - ((1 - WIN_RATE) * actual_risk), 2)

    # ── Risk factors ───────────────────────────────────────────────────────
    risk_factors = _build_risk_factors(signal, sr, df)

    # Days until the next earnings report (best effort from provider info), so
    # RiskGuard can honour the trade_earnings_week safety rule. None when unknown.
    ctx = signal.get("_context")
    days_to_earnings = _days_to_earnings(getattr(ctx, "ticker_info", None))

    plan = {
        "ticker":          ticker,
        "signal":          signal.get("signal", "UNKNOWN"),
        "confidence":      signal.get("confidence", "N/A"),
        "composite_score": signal.get("score", 0.0),
        "days_to_earnings": days_to_earnings,

        "entry": {
            "type":  "LIMIT",
            "price": entry_price,
            "note":  "Set limit order — do NOT use market order",
        },
        "stop_loss": {
            "price":           stop_loss,
            "pct_below_entry": stop_pct,
            "method":          f"{multiplier}x ATR (ATR={atr14:.2f})",
            "note":            "Set immediately after fill. NON-NEGOTIABLE.",
        },
        "targets": targets,

        "position": {
            "shares":         shares,
            "position_value": position_value,
            "risk_dollars":   actual_risk,
            "risk_percent":   actual_risk_pct,
            "pct_of_account": pos_pct_account,
        },

        "expected_value": ev,

        "key_levels": {
            "support":    sr.get("nearest_support"),
            "resistance": sr.get("nearest_resistance"),
        },

        "reasoning":     [r for r in signal.get("reasons", []) if r],
        "risk_factors":  risk_factors,

        "disclaimer": "NOT FINANCIAL ADVICE. All trading involves risk of loss. Paper trade first.",
    }

    return plan


def _days_to_earnings(ticker_info: dict | None) -> int | None:
    """Best-effort days until the next earnings date from provider info.

    Reads the yfinance-style unix-second timestamp keys. Returns None when the
    date is unavailable or in the past — RiskGuard then does NOT block on it.
    """
    if not isinstance(ticker_info, dict):
        return None
    import time as _time

    ts = None
    for key in ("earningsTimestampStart", "earningsTimestamp"):
        v = ticker_info.get(key)
        if v:
            ts = v
            break
    if not ts:
        return None
    try:
        days = int((float(ts) - _time.time()) // 86400)
    except (TypeError, ValueError):
        return None
    return days if days >= 0 else None


def _build_risk_factors(signal: dict, sr: dict, df: pd.DataFrame) -> list:
    factors = []

    resistance = sr.get("nearest_resistance")
    if resistance:
        factors.append(f"Resistance at ${resistance:.2f} — first target may face selling pressure")

    if df is not None and len(df) >= 20:
        vol_20 = float(df["Volume"].tail(20).mean())
        vol_1  = float(df["Volume"].iloc[-1])
        if vol_1 > vol_20 * 3:
            factors.append("Unusual volume spike — check for news catalyst")

    factors.append("S&P 500 correlation — broad market risk applies")

    return factors
