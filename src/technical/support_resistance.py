"""Support & Resistance: Pivot Points and Fibonacci Retracement levels."""

import pandas as pd


def calculate_support_resistance(df: pd.DataFrame, current_price: float) -> dict:
    """
    Compute pivot points and Fibonacci levels, then find nearest support/resistance.

    Pivot Points (Standard):
        PP  = (High + Low + Close) / 3
        R1  = 2*PP - Low
        R2  = PP + (High - Low)
        R3  = High + 2*(PP - Low)
        S1  = 2*PP - High
        S2  = PP - (High - Low)
        S3  = Low - 2*(High - PP)

    Fibonacci (52-week range):
        23.6%, 38.2%, 50.0%, 61.8%, 78.6%
    """
    if df.empty or len(df) < 2:
        return {
            "pivots": {},
            "fibs": {},
            "nearest_support": None,
            "nearest_resistance": None,
            "distance_to_resistance_pct": None,
            "distance_to_support_pct": None,
        }

    # Use prior day's OHLC for pivot calculations
    prev = df.iloc[-2]
    pp   = (prev["High"] + prev["Low"] + prev["Close"]) / 3

    pivots = {
        "PP": pp,
        "R1": 2 * pp - prev["Low"],
        "R2": pp + (prev["High"] - prev["Low"]),
        "R3": prev["High"] + 2 * (pp - prev["Low"]),
        "S1": 2 * pp - prev["High"],
        "S2": pp - (prev["High"] - prev["Low"]),
        "S3": prev["Low"] - 2 * (prev["High"] - pp),
    }

    # Fibonacci retracements from 52-week high/low
    periods = min(252, len(df))
    high_52 = df["High"].iloc[-periods:].max()
    low_52  = df["Low"].iloc[-periods:].min()
    diff    = high_52 - low_52

    fibs = {}
    if diff > 0:
        fibs = {
            "FIB_78.6": high_52 - 0.786 * diff,
            "FIB_61.8": high_52 - 0.618 * diff,
            "FIB_50.0": high_52 - 0.500 * diff,
            "FIB_38.2": high_52 - 0.382 * diff,
            "FIB_23.6": high_52 - 0.236 * diff,
        }

    all_levels = {**pivots, **fibs}
    supports     = {k: v for k, v in all_levels.items() if v < current_price}
    resistances  = {k: v for k, v in all_levels.items() if v > current_price}

    nearest_support    = max(supports.values())    if supports    else None
    nearest_resistance = min(resistances.values()) if resistances else None

    dist_to_res = (
        (nearest_resistance - current_price) / current_price * 100
        if nearest_resistance else None
    )
    dist_to_sup = (
        (current_price - nearest_support) / current_price * 100
        if nearest_support else None
    )

    return {
        "pivots": {k: round(v, 4) for k, v in pivots.items()},
        "fibs":   {k: round(v, 4) for k, v in fibs.items()},
        "nearest_support":    round(nearest_support, 4)    if nearest_support    else None,
        "nearest_resistance": round(nearest_resistance, 4) if nearest_resistance else None,
        "distance_to_resistance_pct": round(dist_to_res, 2) if dist_to_res else None,
        "distance_to_support_pct":    round(dist_to_sup, 2) if dist_to_sup else None,
    }
