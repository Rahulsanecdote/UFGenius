"""Phase 2 tests for canonical provider contracts and context wiring."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.core.models import AssetClass, Fundamentals, Instrument, TickerSnapshot
from src.data.providers import (
    YFinanceTickerSnapshotProvider,
    get_default_ticker_snapshot_provider,
    set_default_ticker_snapshot_provider,
)
from src.signals.context import build_signal_context


def _sample_df(rows: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="D")
    close = np.linspace(100, 120, rows)
    return pd.DataFrame(
        {
            "Open": close * 0.99,
            "High": close * 1.01,
            "Low": close * 0.98,
            "Close": close,
            "Volume": np.full(rows, 500_000.0),
        },
        index=idx,
    )


def test_registry_returns_default_provider_instance():
    set_default_ticker_snapshot_provider(None)
    provider = get_default_ticker_snapshot_provider()
    assert isinstance(provider, YFinanceTickerSnapshotProvider)


def test_yfinance_provider_builds_snapshot_from_prefetched_inputs(monkeypatch):
    provider = YFinanceTickerSnapshotProvider()
    df = _sample_df()
    info = {"currency": "USD", "exchange": "NASDAQ", "longName": "Acme Inc"}

    monkeypatch.setattr(
        provider,
        "get_fundamentals",
        lambda *_args, **_kwargs: {
            "ticker": "ACME",
            "market_cap": 9_000_000_000,
            "pe_ratio": 20.5,
            "peg_ratio": 1.2,
            "revenue_growth_yoy": 0.14,
            "earnings_growth_rate": 0.12,
        },
    )

    snapshot = provider.get_ticker_snapshot("acme", price_df=df, ticker_info=info)
    assert snapshot is not None
    assert snapshot.instrument.symbol == "ACME"
    assert snapshot.instrument.asset_class == AssetClass.EQUITY
    assert snapshot.instrument.exchange == "NASDAQ"
    assert not snapshot.price_df.empty
    assert snapshot.fundamentals.market_cap == 9_000_000_000


@dataclass
class _FakeProvider:
    def get_ticker_snapshot(self, ticker: str, **_kwargs):
        symbol = ticker.upper()
        inst = Instrument(symbol=symbol, provider="fake")
        fund = Fundamentals(instrument=inst, market_cap=1_000_000_000, raw={"market_cap": 1_000_000_000})
        return TickerSnapshot(
            instrument=inst,
            price_df=_sample_df(),
            ticker_info={"longName": "Fake Corp"},
            fundamentals=fund,
        )


def test_build_signal_context_uses_injected_provider(monkeypatch):
    monkeypatch.setattr(
        "src.signals.context.fetch_ohlcv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback fetch should not run")),
    )
    ctx = build_signal_context("fke", provider=_FakeProvider())
    assert ctx is not None
    assert ctx.ticker == "FKE"
    assert ctx.provider == "fake"
    assert ctx.fundamentals_raw.get("market_cap") == 1_000_000_000

