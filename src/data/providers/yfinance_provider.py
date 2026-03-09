"""Default ticker snapshot provider backed by existing yfinance fetch stack."""

from __future__ import annotations

import pandas as pd

from src.core.models import AssetClass, Fundamentals, Instrument, TickerSnapshot
from src.data.fetcher import fetch_ohlcv, fetch_ticker_info
from src.fundamental.fetcher import fetch_fundamentals


class YFinanceTickerSnapshotProvider:
    """Provider adapter that normalizes yfinance/fundamental fetch outputs."""

    provider_name = "yfinance"

    def get_ohlcv(
        self,
        ticker: str,
        *,
        period: str = "1y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        return fetch_ohlcv(ticker, period=period, interval=interval)

    def get_ticker_info(self, ticker: str) -> dict:
        return fetch_ticker_info(ticker)

    def get_fundamentals(self, ticker: str, *, ticker_info: dict | None = None) -> dict:
        return fetch_fundamentals(ticker, info=ticker_info)

    def get_ticker_snapshot(
        self,
        ticker: str,
        *,
        period: str = "1y",
        interval: str = "1d",
        price_df: pd.DataFrame | None = None,
        ticker_info: dict | None = None,
    ) -> TickerSnapshot | None:
        symbol = ticker.upper()
        prices = price_df if price_df is not None else self.get_ohlcv(symbol, period=period, interval=interval)
        if prices is None or prices.empty:
            return None

        info = ticker_info if ticker_info is not None else self.get_ticker_info(symbol)
        fundamentals_raw = self.get_fundamentals(symbol, ticker_info=info)

        instrument = Instrument(
            symbol=symbol,
            asset_class=AssetClass.EQUITY,
            currency=(info or {}).get("currency", "USD"),
            exchange=(info or {}).get("exchange"),
            provider=self.provider_name,
        )
        fundamentals = Fundamentals(
            instrument=instrument,
            market_cap=fundamentals_raw.get("market_cap"),
            pe_ratio=fundamentals_raw.get("pe_ratio"),
            peg_ratio=fundamentals_raw.get("peg_ratio"),
            revenue_growth_yoy=fundamentals_raw.get("revenue_growth_yoy"),
            earnings_growth_rate=fundamentals_raw.get("earnings_growth_rate"),
            raw=fundamentals_raw or {},
        )
        return TickerSnapshot(
            instrument=instrument,
            price_df=prices,
            ticker_info=info or {},
            fundamentals=fundamentals,
        )

