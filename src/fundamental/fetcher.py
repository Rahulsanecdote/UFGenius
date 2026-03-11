"""Fundamental data fetcher — yfinance .info as primary source."""

from __future__ import annotations

from typing import Any

from src.data.fetcher import fetch_ticker_info
from src.utils.logger import get_logger

log = get_logger(__name__)


def fetch_fundamentals(ticker: str, info: dict[str, Any] | None = None) -> dict:
    """
    Fetch fundamental financial data for a ticker.

    Maps yfinance .info keys into a standardised dict.
    Returns a dict with all required fields; missing values default to None.
    """
    info = info if info is not None else fetch_ticker_info(ticker)
    if not info:
        return _empty_fundamentals()

    def _get(*keys, default=None):
        for k in keys:
            v = info.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return default

    price      = _get("currentPrice", "regularMarketPrice", "previousClose", "lastPrice", "last_price", "previous_close")
    market_cap = _get("marketCap", "market_cap")
    shares     = _get("sharesOutstanding", "shares")

    liabilities = _get("totalLiab", "totalLiabilities", "totalDebt")
    total_equity = None
    if shares is not None and shares > 0:
        bvps = _get("bookValue")
        if bvps is not None:
            total_equity = bvps * shares

    return {
        "ticker":        ticker,
        "price":         price,
        "market_cap":    market_cap,
        "shares_outstanding": shares,

        # Income Statement
        "revenue":             _get("totalRevenue"),
        "gross_profit":        _get("grossProfits"),
        "ebit":                _get("ebit"),
        "ebitda":              _get("ebitda"),
        "net_income":          _get("netIncomeToCommon"),
        "eps":                 _get("trailingEps", "forwardEps"),

        # Balance Sheet
        "total_assets":        _get("totalAssets"),
        "total_liabilities":   liabilities,
        "total_equity":        total_equity,
        "current_assets":      _get("totalCurrentAssets"),
        "current_liabilities": _get("totalCurrentLiabilities"),
        "retained_earnings":   _get("retainedEarnings"),
        "total_debt":          _get("totalDebt"),
        "book_value_per_share": _get("bookValue"),

        # Cash Flow
        "operating_cash_flow": _get("operatingCashflow"),
        "free_cash_flow":      _get("freeCashflow"),

        # Enterprise Value
        "enterprise_value":    _get("enterpriseValue"),

        # Growth (YoY rates as decimals)
        "revenue_growth_yoy":  _get("revenueGrowth"),
        "earnings_growth_rate": _get("earningsGrowth", "earningsQuarterlyGrowth"),
        "eps_growth_yoy":      _get("earningsGrowth"),
        "fcf_growth_yoy":      None,  # Not directly available from yfinance

        # Ratios
        "pe_ratio":            _get("trailingPE", "forwardPE"),
        "peg_ratio":           _get("pegRatio"),
        "ps_ratio":            _get("priceToSalesTrailing12Months"),
        "pb_ratio":            _get("priceToBook"),

        # Previous period (yfinance doesn't always have these)
        "net_income_prev":     None,
        "total_assets_prev":   None,
        "revenue_prev":        None,
    }


def _empty_fundamentals() -> dict:
    """Return a dict of all-None fundamentals."""
    return {k: None for k in [
        "ticker", "price", "market_cap", "shares_outstanding",
        "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps",
        "total_assets", "total_liabilities", "total_equity",
        "current_assets", "current_liabilities", "retained_earnings",
        "total_debt", "book_value_per_share",
        "operating_cash_flow", "free_cash_flow", "enterprise_value",
        "revenue_growth_yoy", "earnings_growth_rate", "eps_growth_yoy", "fcf_growth_yoy",
        "pe_ratio", "peg_ratio", "ps_ratio", "pb_ratio",
        "net_income_prev", "total_assets_prev", "revenue_prev",
    ]}
