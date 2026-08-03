"""Disqualification filters — hard STOPs that override any positive signal."""

import pandas as pd

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Module-level price floor (kept as an overridable attribute). Penny mode drops
# the effective floor to 0 so sub-$1 names are allowed.
MIN_PRICE = max(0.0, config.SIGNAL_MIN_PRICE)


def _thresholds() -> dict:
    """Resolve hard-stop thresholds from config at call time.

    Sourced from config.yaml (`filter_*` keys), relaxed when
    ALLOW_PENNY_STOCKS is set. Evaluated per-call (not frozen at import) so
    config/env changes take effect and the values stay testable (audit H4).
    """
    penny = config.ALLOW_PENNY_STOCKS
    return {
        "min_price":         0.0 if penny else MIN_PRICE,
        "min_avg_volume":    10_000 if penny else config.FILTER_MIN_AVG_VOLUME,
        "min_market_cap":    0 if penny else config.FILTER_MIN_MARKET_CAP,
        "max_5day_gain_pct": 100.0 if penny else config.FILTER_MAX_5DAY_GAIN_PCT,
        "bankruptcy_z":      0.0 if penny else config.FILTER_BANKRUPTCY_Z,
    }


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
    t = _thresholds()

    if df.empty:
        reasons.append("NO_DATA: Unable to fetch price data")
        return reasons

    current_price = float(df["Close"].iloc[-1])

    # Price floor
    if current_price < t["min_price"]:
        reasons.append(f"PENNY_STOCK: Price ${current_price:.2f} < ${t['min_price']}")

    # Volume floor
    avg_vol_20 = df["Volume"].tail(20).mean()
    if avg_vol_20 < t["min_avg_volume"]:
        reasons.append(f"ILLIQUID: Avg vol {avg_vol_20:,.0f} < {t['min_avg_volume']:,.0f}")

    # Altman Z-Score bankruptcy risk
    z_score = fundamental_score.get("altman_z_score")
    if z_score is not None and z_score < t["bankruptcy_z"]:
        reasons.append(f"BANKRUPTCY_RISK: Z-Score {z_score:.2f} < {t['bankruptcy_z']}")

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
    elif market_cap < t["min_market_cap"]:
        reasons.append(
            f"MICRO_CAP: Market cap ${market_cap:,.0f} < ${t['min_market_cap']:,.0f}"
        )

    # 5-day surge (chaser trap)
    if len(df) >= 6:
        price_5d_ago = float(df["Close"].iloc[-6])
        if price_5d_ago > 0:
            gain_5d = (current_price / price_5d_ago - 1) * 100
            if gain_5d > t["max_5day_gain_pct"]:
                reasons.append(
                    f"CHASER_TRAP: Already up {gain_5d:.0f}% in 5 days (max {t['max_5day_gain_pct']}%)"
                )

    return reasons
