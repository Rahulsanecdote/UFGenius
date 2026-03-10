"""Shared typed context for signal/scanner pipeline to avoid duplicate fetches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.contracts import TickerSnapshotProvider
from src.core.models import Instrument
from src.data.fetcher import fetch_ohlcv, fetch_ticker_info
from src.data.providers import get_default_ticker_snapshot_provider
from src.fundamental.fetcher import fetch_fundamentals


@dataclass
class SignalContext:
    ticker: str
    price_df: pd.DataFrame
    ticker_info: dict[str, Any]
    fundamentals_raw: dict[str, Any]
    instrument: Instrument | None = None
    provider: str | None = None


def build_signal_context(
    ticker: str,
    *,
    price_df: pd.DataFrame | None = None,
    ticker_info: dict[str, Any] | None = None,
    provider: TickerSnapshotProvider | None = None,
) -> SignalContext | None:
    symbol = ticker.upper()
    explicit_provider = provider is not None

    if provider is None:
        provider = get_default_ticker_snapshot_provider()

    # Preserve fast/prefetched scanner path: when caller already has price_df and
    # did not explicitly request a provider adapter, use legacy local fetch helpers.
    use_provider = explicit_provider or (price_df is None and ticker_info is None)

    if provider is not None and use_provider:
        snapshot = provider.get_ticker_snapshot(
            symbol,
            period="1y",
            interval="1d",
            price_df=price_df,
            ticker_info=ticker_info,
        )
        if snapshot is not None and snapshot.price_df is not None and not snapshot.price_df.empty:
            return SignalContext(
                ticker=symbol,
                price_df=snapshot.price_df,
                ticker_info=snapshot.ticker_info or {},
                fundamentals_raw=snapshot.fundamentals_raw or {},
                instrument=snapshot.instrument,
                provider=snapshot.instrument.provider,
            )

    # Backward-compatibility fallback path.
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
        instrument=Instrument(symbol=symbol),
        provider=None,
    )
