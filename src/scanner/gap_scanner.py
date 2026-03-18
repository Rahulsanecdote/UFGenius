"""
Gap scanner — detect pre-market gaps and high-volume breakouts.

Uses previous close vs current open to find gappers.
Uses volume vs 20-day average to find breakouts.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.data.fetcher import fetch_ohlcv
from src.utils.logger import get_logger

log = get_logger(__name__)


def scan_for_gaps(
    tickers: list[str],
    min_gap_pct: float = 5.0,
    min_volume_ratio: float = 2.0,
) -> list[dict]:
    """
    Scan tickers for gap-up or gap-down openings.

    A gap is when today's open is significantly different from yesterday's close.

    Args:
        tickers: List of ticker symbols to scan.
        min_gap_pct: Minimum gap percentage to qualify (default 5%).
        min_volume_ratio: Minimum volume vs 20-day avg (default 2x).

    Returns list of gap dicts sorted by absolute gap percentage descending.
    """
    results = []

    for ticker in tickers:
        try:
            df = fetch_ohlcv(ticker, period="1mo", interval="1d")
            if df is None or df.empty or len(df) < 5:
                continue

            prev_close = float(df["Close"].iloc[-2])
            today_open = float(df["Open"].iloc[-1])
            today_close = float(df["Close"].iloc[-1])
            today_volume = float(df["Volume"].iloc[-1])

            if prev_close <= 0:
                continue

            gap_pct = ((today_open - prev_close) / prev_close) * 100

            # Volume ratio vs 20-day average
            avg_vol_20 = df["Volume"].tail(20).mean()
            vol_ratio = today_volume / avg_vol_20 if avg_vol_20 > 0 else 0

            if abs(gap_pct) >= min_gap_pct:
                # Gap fill analysis
                if gap_pct > 0:
                    gap_filled = today_close <= prev_close
                    gap_filling = today_close < today_open
                else:
                    gap_filled = today_close >= prev_close
                    gap_filling = today_close > today_open

                results.append({
                    "ticker": ticker,
                    "gap_pct": round(gap_pct, 2),
                    "direction": "UP" if gap_pct > 0 else "DOWN",
                    "prev_close": round(prev_close, 4),
                    "open": round(today_open, 4),
                    "current": round(today_close, 4),
                    "volume": int(today_volume),
                    "volume_ratio": round(vol_ratio, 1),
                    "gap_filled": gap_filled,
                    "gap_filling": gap_filling,
                    "high_volume": vol_ratio >= min_volume_ratio,
                })

        except Exception as e:
            log.debug(f"{ticker}: gap scan error: {e}")
            continue

    results.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    return results


def scan_for_volume_breakouts(
    tickers: list[str],
    min_volume_ratio: float = 3.0,
    min_price_change_pct: float = 3.0,
) -> list[dict]:
    """
    Find tickers with unusual volume AND price movement.

    A volume breakout is when today's volume is 3x+ the 20-day average
    AND price moved 3%+ from the previous close.

    Args:
        tickers: List of ticker symbols.
        min_volume_ratio: Volume vs 20-day average threshold.
        min_price_change_pct: Minimum price change to qualify.

    Returns list of breakout dicts sorted by volume ratio descending.
    """
    results = []

    for ticker in tickers:
        try:
            df = fetch_ohlcv(ticker, period="1mo", interval="1d")
            if df is None or df.empty or len(df) < 21:
                continue

            current = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            today_volume = float(df["Volume"].iloc[-1])
            avg_vol_20 = float(df["Volume"].tail(20).mean())

            if prev_close <= 0 or avg_vol_20 <= 0:
                continue

            price_change_pct = ((current - prev_close) / prev_close) * 100
            vol_ratio = today_volume / avg_vol_20

            if vol_ratio >= min_volume_ratio and abs(price_change_pct) >= min_price_change_pct:
                # Check if this is a breakout above resistance
                high_20 = float(df["High"].tail(20).max())
                is_new_high = current >= high_20

                results.append({
                    "ticker": ticker,
                    "price": round(current, 4),
                    "change_pct": round(price_change_pct, 2),
                    "direction": "BULLISH" if price_change_pct > 0 else "BEARISH",
                    "volume": int(today_volume),
                    "avg_volume_20": int(avg_vol_20),
                    "volume_ratio": round(vol_ratio, 1),
                    "is_new_20d_high": is_new_high,
                })

        except Exception as e:
            log.debug(f"{ticker}: breakout scan error: {e}")
            continue

    results.sort(key=lambda x: x["volume_ratio"], reverse=True)
    return results
