"""Data provider adapters and registry."""

from .registry import get_default_ticker_snapshot_provider, set_default_ticker_snapshot_provider
from .yfinance_provider import YFinanceTickerSnapshotProvider

__all__ = [
    "YFinanceTickerSnapshotProvider",
    "get_default_ticker_snapshot_provider",
    "set_default_ticker_snapshot_provider",
]

