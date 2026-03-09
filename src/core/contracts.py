"""Provider contracts for canonical data ingestion interfaces."""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from src.core.models import TickerSnapshot


class OhlcvProvider(Protocol):
    def get_ohlcv(
        self,
        ticker: str,
        *,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return OHLCV dataframe for ticker."""


class TickerInfoProvider(Protocol):
    def get_ticker_info(self, ticker: str) -> dict:
        """Return ticker metadata/info dict."""


class FundamentalsProvider(Protocol):
    def get_fundamentals(self, ticker: str, *, ticker_info: dict | None = None) -> dict:
        """Return canonical raw fundamentals mapping."""


class TickerSnapshotProvider(OhlcvProvider, TickerInfoProvider, FundamentalsProvider, Protocol):
    def get_ticker_snapshot(
        self,
        ticker: str,
        *,
        period: str = "1y",
        interval: str = "1d",
        price_df: pd.DataFrame | None = None,
        ticker_info: dict | None = None,
    ) -> TickerSnapshot | None:
        """Return canonical typed snapshot for signal pipeline."""

