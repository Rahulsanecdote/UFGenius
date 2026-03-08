"""Disqualification filters — hard STOPs that override any positive signal."""

from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from src.data import cache
from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

# Hard stop thresholds
MIN_PRICE          = 1.0          # Penny stock floor
MIN_AVG_VOLUME     = 100_000      # Illiquidity floor
MIN_MARKET_CAP     = 100_000_000  # Nano-cap trap floor
MAX_5DAY_GAIN_PCT  = 50.0         # Chaser trap ceiling
BANKRUPTCY_Z       = 1.0          # Altman Z-Score bankruptcy floor
EARNINGS_BUFFER_DAYS = 5          # Days before/after earnings to avoid


def _get_next_earnings_date(ticker: str) -> datetime | None:
    """Return the next earnings date for ticker, or None if unknown. Cached 12h."""
    cache_key = f"earnings:{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # may be None (stored explicitly as "no date found")

    try:
        cal = yf.Ticker(ticker).calendar
        if not cal:
            cache.set(cache_key, None, ttl=43_200)
            return None

        # calendar is a dict with key 'Earnings Date' → list of Timestamps
        dates = cal.get("Earnings Date") or []
        if not dates:
            cache.set(cache_key, None, ttl=43_200)
            return None

        # Pick the soonest future-or-past earnings date
        now = datetime.now(tz=timezone.utc)
        ts_list = []
        for d in dates:
            if hasattr(d, "to_pydatetime"):
                ts_list.append(d.to_pydatetime())
            elif isinstance(d, datetime):
                ts_list.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc))

        # Return the closest upcoming date (or most recent past if all past)
        if not ts_list:
            cache.set(cache_key, None, ttl=43_200)
            return None

        upcoming = [t for t in ts_list if t >= now]
        result = min(upcoming) if upcoming else max(ts_list)

        cache.set(cache_key, result, ttl=43_200)
        return result

    except Exception as e:
        log.debug(f"{ticker}: earnings date fetch failed: {e}")
        cache.set(cache_key, None, ttl=43_200)
        return None


def run_disqualification_filters(
    ticker: str,
    df: pd.DataFrame,
    fundamental: dict,
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
    ✗ Earnings within buffer days     (binary event risk)
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
    z_score = fundamental.get("altman_z_score")
    if z_score is not None and z_score < BANKRUPTCY_Z:
        reasons.append(f"BANKRUPTCY_RISK: Z-Score {z_score:.2f} < {BANKRUPTCY_Z}")

    # 5-day surge (chaser trap)
    if len(df) >= 6:
        price_5d_ago = float(df["Close"].iloc[-6])
        if price_5d_ago > 0:
            gain_5d = (current_price / price_5d_ago - 1) * 100
            if gain_5d > MAX_5DAY_GAIN_PCT:
                reasons.append(
                    f"CHASER_TRAP: Already up {gain_5d:.0f}% in 5 days (max {MAX_5DAY_GAIN_PCT}%)"
                )

    # Earnings proximity — skip if trade_earnings_week is explicitly enabled
    if not config.SAFETY.get("trade_earnings_week", False):
        earnings_dt = _get_next_earnings_date(ticker)
        if earnings_dt is not None:
            now = datetime.now(tz=timezone.utc)
            delta_days = abs((earnings_dt - now).days)
            if delta_days <= EARNINGS_BUFFER_DAYS:
                reasons.append(
                    f"EARNINGS_RISK: Earnings in {delta_days}d "
                    f"({earnings_dt.strftime('%Y-%m-%d')}) — binary event, skip"
                )

    return reasons
