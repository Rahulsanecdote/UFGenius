"""Canonical typed models shared across data/signal/risk pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pandas as pd


class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    FOREX = "forex"
    OPTION = "option"
    COMMODITY = "commodity"
    INDEX = "index"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    currency: str = "USD"
    exchange: str | None = None
    provider: str | None = None

    def normalized_symbol(self) -> str:
        return self.symbol.upper().strip()


@dataclass
class Quote:
    instrument: Instrument
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Fundamentals:
    instrument: Instrument
    market_cap: float | None = None
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    revenue_growth_yoy: float | None = None
    earnings_growth_rate: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.raw)
        data.setdefault("ticker", self.instrument.normalized_symbol())
        data["market_cap"] = self.market_cap
        data["pe_ratio"] = self.pe_ratio
        data["peg_ratio"] = self.peg_ratio
        data["revenue_growth_yoy"] = self.revenue_growth_yoy
        data["earnings_growth_rate"] = self.earnings_growth_rate
        return data


@dataclass
class TickerSnapshot:
    instrument: Instrument
    price_df: pd.DataFrame
    ticker_info: dict[str, Any]
    fundamentals: Fundamentals
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fundamentals_raw(self) -> dict[str, Any]:
        return self.fundamentals.to_dict()

