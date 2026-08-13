"""Fundamental data fetcher — yfinance .info primary, FMP fallback.

yfinance is the primary source, but its ``.info`` endpoint rate-limits hard;
when it returns nothing (so ``market_cap`` is unknown and the disqualification
filter would reject the ticker), fall back to Financial Modeling Prep — which
the operator already supplies a key for — to fill the gaps. Mirrors the
multi-provider fallback the price layer (``src/data/fetcher``) already has.
"""

from __future__ import annotations

from typing import Any

from src.data.fetcher import fetch_ticker_info
from src.utils import config
from src.utils.http import get_retry_session
from src.utils.logger import get_logger

log = get_logger(__name__)

# FMP's current ("stable") quote endpoint. The legacy /api/v3/quote path now
# returns 403 for keys issued after Aug 2025, so use /stable with ?symbol=.
_FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"


def fetch_fundamentals(ticker: str, info: dict[str, Any] | None = None) -> dict:
    """
    Fetch fundamental financial data for a ticker.

    Maps yfinance .info keys into a standardised dict.
    Returns a dict with all required fields; missing values default to None.
    """
    info = info if info is not None else fetch_ticker_info(ticker)
    if not info:
        # yfinance gave nothing (commonly a rate-limit). Try FMP outright rather
        # than returning all-None, which would trip UNKNOWN_MARKET_CAP.
        fmp = _fetch_fmp_fundamentals(ticker)
        if fmp:
            base = _empty_fundamentals()
            base.update(fmp)
            base["ticker"] = ticker
            return base
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

    # Accept both yfinance .info keys and fast_info-style aliases
    # (last_price / market_cap / shares) so either payload shape works.
    price      = _get("currentPrice", "regularMarketPrice", "previousClose", "last_price")
    market_cap = _get("marketCap", "market_cap")
    shares     = _get("sharesOutstanding", "shares")

    liabilities = _get("totalLiab", "totalLiabilities", "totalDebt")
    total_equity = None
    if shares is not None and shares > 0:
        bvps = _get("bookValue")
        if bvps is not None:
            total_equity = bvps * shares

    result: dict = {
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

    # Fill gaps from FMP when yfinance omitted the market cap (the field the
    # disqualification filter hard-requires). Only fills values that are still
    # None — yfinance stays authoritative for whatever it did return.
    if result.get("market_cap") is None:
        fmp = _fetch_fmp_fundamentals(ticker)
        for key, value in fmp.items():
            if value is not None and result.get(key) is None:
                result[key] = value

    return result


def _fetch_fmp_fundamentals(ticker: str) -> dict:
    """Best-effort fundamentals from Financial Modeling Prep (/quote).

    Returns a partial fundamentals dict (only the fields FMP's quote supplies),
    or {} when no key is configured or the request fails. Never raises — this is
    a fallback, so any error just yields no fill.
    """
    key = config.FMP_KEY
    if not key:
        return {}
    try:
        resp = get_retry_session().get(
            _FMP_QUOTE_URL,
            params={"symbol": ticker, "apikey": key},
            timeout=(config.REQUEST_CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # network / JSON / HTTP — fallback must never raise
        log.warning(f"{ticker}: FMP fundamentals fallback failed ({type(exc).__name__})")
        return {}

    # FMP /quote returns a list with a single object.
    row = payload[0] if isinstance(payload, list) and payload else None
    if not isinstance(row, dict):
        return {}

    def _num(*keys):
        for k in keys:
            v = row.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    out = {
        "price":              _num("price"),
        "market_cap":         _num("marketCap"),
        "shares_outstanding": _num("sharesOutstanding"),
        "eps":                _num("eps"),
        "pe_ratio":           _num("pe"),
    }
    if out.get("market_cap") is not None:
        log.info(f"{ticker}: market cap filled from FMP fallback")
    # Drop Nones so the caller's gap-fill only sees real values.
    return {k: v for k, v in out.items() if v is not None}


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
