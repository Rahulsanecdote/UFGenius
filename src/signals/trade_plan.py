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

import pandas as pd

from src.technical.support_resistance import calculate_support_resistance
from src.technical.volatility import calculate_volatility_indicators
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

WIN_RATE  = config.EV_WIN_RATE   # Historical estimate — see config.yaml: ev_win_rate
AVG_RR    = config.EV_AVG_RR     # Historical estimate — see config.yaml: ev_avg_rr


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
            atr14 = float(v) if not pd.isna(v) else None

    if atr14 is None or atr14 == 0:
        atr14 = current_price * 0.02  # Fallback: 2% of price

    # ── Support / Resistance ───────────────────────────────────────────────
    sr = signal.get("support_resistance") or calculate_support_resistance(df, current_price)

    # ── Entry ──────────────────────────────────────────────────────────────
    entry_price = round(current_price * 0.998, 2)  # Slight discount for limit order

    # ── Stop Loss ──────────────────────────────────────────────────────────
    multiplier = config.ATR_STOP_MULTIPLIER
    stop_loss  = round(entry_price - atr14 * multiplier, 2)
    stop_pct   = round((entry_price - stop_loss) / entry_price * 100, 2)

    # ── Targets ────────────────────────────────────────────────────────────
    risk = entry_price - stop_loss
    rr_ratios  = config.TARGET_RR_RATIOS   # [1.5, 2.5, 4.0]
    exit_pcts  = config.TARGET_EXIT_PCTS   # [30, 40, 30]

    raw_targets = [round(entry_price + risk * rr, 2) for rr in rr_ratios]

    # Snap T1 to nearest resistance if it's between entry and T2
    # Enter slightly below resistance (configurable discount) to allow for spread/slippage
    nearest_res = sr.get("nearest_resistance")
    if nearest_res and entry_price < nearest_res < raw_targets[1]:
        raw_targets[0] = round(float(nearest_res) * config.RESISTANCE_SNAP_DISCOUNT, 2)

    targets = {}
    labels = ["T1", "T2", "T3"]
    for i, (label, price, rr, ep) in enumerate(
        zip(labels, raw_targets, rr_ratios, exit_pcts)
    ):
        targets[label] = {
            "price":    price,
            "exit_pct": ep,
            "rr":       f"{rr}:1",
        }

    # ── Position Sizing ────────────────────────────────────────────────────
    risk_pct    = config.RISK_PER_TRADE   # e.g. 0.01 = 1%
    max_pos_pct = config.MAX_POSITION_PCT # e.g. 0.10 = 10%

    if risk <= 0:
        log.warning(
            f"{ticker}: entry price equals stop loss (risk=0); "
            "using max-position sizing only — verify ATR and stop multiplier"
        )

    risk_dollars     = account_size * risk_pct
    shares_by_risk   = risk_dollars / risk if risk > 0 else float("inf")
    shares_by_max    = (account_size * max_pos_pct) / entry_price
    raw_shares       = int(min(shares_by_risk, shares_by_max))
    shares           = max(raw_shares, 1)
    if shares > raw_shares:
        log.warning(
            f"{ticker}: position clamped to minimum 1 share "
            f"(computed {raw_shares} shares); check account size and risk settings"
        )

    position_value  = round(shares * entry_price, 2)
    actual_risk     = round(shares * risk, 2)
    actual_risk_pct = round(actual_risk / account_size * 100, 2)
    pos_pct_account = round(position_value / account_size * 100, 2)

    # ── Expected Value ─────────────────────────────────────────────────────
    ev = round((WIN_RATE * AVG_RR * actual_risk) - ((1 - WIN_RATE) * actual_risk), 2)

    # ── Risk factors ───────────────────────────────────────────────────────
    risk_factors = _build_risk_factors(signal, sr, df)

    plan = {
        "ticker":          ticker,
        "signal":          signal.get("signal", "UNKNOWN"),
        "confidence":      signal.get("confidence", "N/A"),
        "composite_score": signal.get("score", 0.0),

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
