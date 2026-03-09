"""Provider registry for dependency-injected data access."""

from __future__ import annotations

from src.core.contracts import TickerSnapshotProvider
from src.data.providers.yfinance_provider import YFinanceTickerSnapshotProvider

_DEFAULT_TICKER_SNAPSHOT_PROVIDER: TickerSnapshotProvider | None = None


def get_default_ticker_snapshot_provider() -> TickerSnapshotProvider:
    global _DEFAULT_TICKER_SNAPSHOT_PROVIDER
    if _DEFAULT_TICKER_SNAPSHOT_PROVIDER is None:
        _DEFAULT_TICKER_SNAPSHOT_PROVIDER = YFinanceTickerSnapshotProvider()
    return _DEFAULT_TICKER_SNAPSHOT_PROVIDER


def set_default_ticker_snapshot_provider(provider: TickerSnapshotProvider | None) -> None:
    """Override default provider for tests/custom runtime wiring."""
    global _DEFAULT_TICKER_SNAPSHOT_PROVIDER
    _DEFAULT_TICKER_SNAPSHOT_PROVIDER = provider

