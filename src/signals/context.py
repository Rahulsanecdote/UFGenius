"""Shared typed context for signal/scanner pipeline to avoid duplicate fetches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.data.fetcher import fetch_ohlcv, fetch_ticker_info
from src.fundamental.fetcher import fetch_fundamentals


@dataclass
class SignalContext:
    ticker: str
    price_df: pd.DataFrame
    ticker_info: dict[str, Any]
    fundamentals_raw: dict[str, Any]


def build_signal_context(
    ticker: str,
    *,
    price_df: pd.DataFrame | None = None,
    ticker_info: dict[str, Any] | None = None,
) -> SignalContext | None:
    symbol = ticker.upper()
    prices = price_df if price_df is not None else fetch_ohlcv(symbol, period="1y")
    if prices is None or prices.empty:
        return None
    info = ticker_info if ticker_info is not None else fetch_ticker_info(symbol)
    fundamentals = fetch_fundamentals(symbol, info=info)
    return SignalContext(
        ticker=symbol,
        price_df=prices,
        ticker_info=info or {},
        fundamentals_raw=fundamentals,
    )
