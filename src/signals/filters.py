"""Disqualification filters — hard STOPs that override any positive signal."""

import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)

# Hard stop thresholds
MIN_PRICE          = 1.0          # Penny stock floor
MIN_AVG_VOLUME     = 100_000      # Illiquidity floor
MIN_MARKET_CAP     = 100_000_000  # Nano-cap trap floor
MAX_5DAY_GAIN_PCT  = 50.0         # Chaser trap ceiling
BANKRUPTCY_Z       = 1.0          # Altman Z-Score bankruptcy floor


def run_disqualification_filters(
    ticker: str,
    df: pd.DataFrame,
    fundamental_score: dict,
    fundamentals_raw: dict | None = None,
) -> list:
    """
    Return a list of disqualification reasons.
    An empty list means the ticker passes all hard filters.

    Checks:
    ✗ Altman Z-Score < 1.0           (bankruptcy risk)
    ✗ Price < $1.00                   (penny stock)
    ✗ Avg 20-day volume < 100K        (illiquid)
    ✗ Already up >50% in 5 days       (chaser trap)
    ✗ Market cap < $100M              (nano-cap)
    """
    reasons = []

    if df.empty:
        reasons.append("NO_DATA: Unable to fetch price data")
        return reasons

    current_price = float(df["Close"].iloc[-1])

    # Price floor
    if current_price < MIN_PRICE:
        reasons.append(f"PENNY_STOCK: Price ${current_price:.2f} < ${MIN_PRICE}")

    # Volume floor
    avg_vol_20 = df["Volume"].tail(20).mean()
    if avg_vol_20 < MIN_AVG_VOLUME:
        reasons.append(f"ILLIQUID: Avg vol {avg_vol_20:,.0f} < {MIN_AVG_VOLUME:,}")

    # Altman Z-Score bankruptcy risk
    z_score = fundamental_score.get("altman_z_score")
    if z_score is not None and z_score < BANKRUPTCY_Z:
        reasons.append(f"BANKRUPTCY_RISK: Z-Score {z_score:.2f} < {BANKRUPTCY_Z}")

    # Market cap from raw fundamentals is canonical source
    market_cap = None
    if isinstance(fundamentals_raw, dict):
        market_cap = fundamentals_raw.get("market_cap")
    if market_cap is None and isinstance(fundamental_score, dict):
        market_cap = fundamental_score.get("market_cap")
        if market_cap is None:
            market_cap = (
                fundamental_score.get("raw_fundamentals", {}) or {}
            ).get("market_cap")

    try:
        market_cap = float(market_cap) if market_cap is not None else None
    except (TypeError, ValueError):
        market_cap = None

    if market_cap is None:
        reasons.append("UNKNOWN_MARKET_CAP: Unable to verify market cap")
    elif market_cap < MIN_MARKET_CAP:
        reasons.append(
            f"MICRO_CAP: Market cap ${market_cap:,.0f} < ${MIN_MARKET_CAP:,.0f}"
        )

    # 5-day surge (chaser trap)
    if len(df) >= 6:
        price_5d_ago = float(df["Close"].iloc[-6])
        if price_5d_ago > 0:
            gain_5d = (current_price / price_5d_ago - 1) * 100
            if gain_5d > MAX_5DAY_GAIN_PCT:
                reasons.append(
                    f"CHASER_TRAP: Already up {gain_5d:.0f}% in 5 days (max {MAX_5DAY_GAIN_PCT}%)"
                )

    return reasons
