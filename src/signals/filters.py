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

    Sourced from config.yaml (`filter_*` keys). When ALLOW_PENNY_STOCKS is set,
    the standard disqualifiers are swapped for penny-specific **hard rails**
    (`penny.*` / `PENNY_*`) — this is deliberately NOT a "remove all protection"
    mode: the market-cap floor stays positive, a dollar-volume gate is added, a
    price band is enforced, the chaser-trap tightens, and the **bankruptcy check
    stays on**. Evaluated per-call (not frozen at import) so config/env changes
    take effect and the values stay testable (audit H4).
    """
    if config.ALLOW_PENNY_STOCKS:
        return {
            "min_price":         max(0.0, config.PENNY_MIN_PRICE),
            "max_price":         config.PENNY_MAX_PRICE,
            "min_avg_volume":    config.PENNY_MIN_SHARE_VOLUME,
            "min_dollar_volume": config.PENNY_MIN_DOLLAR_VOLUME,
            "min_market_cap":    config.PENNY_MIN_MARKET_CAP,
            "max_5day_gain_pct": config.PENNY_MAX_5DAY_GAIN_PCT,
            "bankruptcy_z":      config.FILTER_BANKRUPTCY_Z,  # NOT disabled in penny mode
        }
    return {
        "min_price":         MIN_PRICE,
        "max_price":         float("inf"),   # no ceiling in standard mode
        "min_avg_volume":    config.FILTER_MIN_AVG_VOLUME,
        "min_dollar_volume": 0.0,            # dollar-volume gate is penny-mode only
        "min_market_cap":    config.FILTER_MIN_MARKET_CAP,
        "max_5day_gain_pct": config.FILTER_MAX_5DAY_GAIN_PCT,
        "bankruptcy_z":      config.FILTER_BANKRUPTCY_Z,
    }


def run_disqualification_filters(
    ticker: str,
    df: pd.DataFrame,
    fundamental_score: dict,
    fundamentals_raw: dict | None = None,
    *,
    point_in_time: bool = False,
) -> list:
    """
    Return a list of disqualification reasons.
    An empty list means the ticker passes all hard filters.

    Checks (standard mode; penny mode swaps in the `penny.*` hard rails):
    ✗ Altman Z-Score < 1.0           (bankruptcy risk — enforced in penny mode too)
    ✗ Price < $1.00                   (penny stock; penny mode uses a $0.50 floor)
    ✗ Price > band ceiling            (penny mode only, default $10)
    ✗ Avg 20-day volume < 100K        (illiquid — share count)
    ✗ Dollar volume < $3M/day         (penny mode only — the real liquidity gate)
    ✗ Already up >50% in 5 days       (chaser trap; penny mode tightens to 30%)
    ✗ Market cap < $100M              (nano-cap; penny mode floors at $50M, not 0)
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

    # Price ceiling (penny-band mode only; inf in standard mode)
    if current_price > t["max_price"]:
        reasons.append(
            f"ABOVE_PRICE_BAND: Price ${current_price:.2f} > ${t['max_price']:.2f}"
        )

    # Share-volume floor
    avg_vol_20 = float(df["Volume"].tail(20).mean())
    if avg_vol_20 < t["min_avg_volume"]:
        reasons.append(f"ILLIQUID: Avg vol {avg_vol_20:,.0f} < {t['min_avg_volume']:,.0f}")

    # Dollar-volume floor (penny hard rail; 0 = disabled). price x avg volume is
    # the liquidity gate that a share count misses: it rejects both huge-share
    # sub-penny names and high-price zero-volume names (both common on daily-
    # gainer screens) that you could never actually get filled on.
    if t["min_dollar_volume"] > 0:
        dollar_vol = current_price * avg_vol_20
        if dollar_vol < t["min_dollar_volume"]:
            reasons.append(
                f"THIN_DOLLAR_VOLUME: ${dollar_vol:,.0f}/day < "
                f"${t['min_dollar_volume']:,.0f}"
            )

    # ── Fundamentals-derived checks ──────────────────────────────────────────
    # These read the balance sheet and market cap, which providers serve only as
    # of *today*. In point-in-time mode (the backtest) there is no honest value
    # for a historical bar: using today's would be look-ahead, and the live
    # fail-closed behaviour (unknown market cap ⇒ disqualify) would reject every
    # ticker on every bar. So they are skipped, and the caller discloses it.
    # Live callers never pass point_in_time, so their behaviour is unchanged.
    if point_in_time:
        return reasons + _price_derived_surge_reasons(df, current_price, t)

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

    return reasons + _price_derived_surge_reasons(df, current_price, t)


def _price_derived_surge_reasons(df: pd.DataFrame, current_price: float, t: dict) -> list:
    """5-day surge (chaser trap) — derived purely from the price frame.

    Shared by the live and point-in-time paths so the two cannot drift apart.
    """
    reasons: list = []
    if len(df) >= 6:
        price_5d_ago = float(df["Close"].iloc[-6])
        if price_5d_ago > 0:
            gain_5d = (current_price / price_5d_ago - 1) * 100
            if gain_5d > t["max_5day_gain_pct"]:
                reasons.append(
                    f"CHASER_TRAP: Already up {gain_5d:.0f}% in 5 days (max {t['max_5day_gain_pct']}%)"
                )
    return reasons
