"""Fundamental data fetcher — ticker info primary, then market-cap fallbacks.

Ticker info comes from ``src.data.fetcher.fetch_ticker_info`` (Alpaca first when
its keys are set, yfinance otherwise). When it carries no ``market_cap`` — which
with Alpaca keys is *always*, since Alpaca's asset record has no such field —
the disqualification filter would reject the ticker as UNKNOWN_MARKET_CAP. So
the market cap alone is resolved down a chain, cheapest first:

    ticker info → last known value (disk) → FMP (keyed) → SEC shares × price
    (keyless) → Finviz (opt-in)

The source that answered is recorded as ``market_cap_source``. See
``src/fundamental/market_cap.py`` for the measurement that made this necessary.
"""

from __future__ import annotations

from typing import Any

from src.data.fetcher import fetch_ticker_info
from src.fundamental import market_cap
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
        # Ticker info gave nothing (commonly a rate-limit). Resolve the market
        # cap down the fallback chain rather than returning all-None, which would
        # trip UNKNOWN_MARKET_CAP; Finviz then fills anything still missing.
        base = _fill_market_cap(ticker, _empty_fundamentals())
        if base.get("market_cap") is not None:
            base["ticker"] = ticker
        return _backfill_from_finviz(ticker, base)

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

    # Source precedence: ticker info is authoritative; the market-cap chain
    # fills what it omitted; Finviz fills last, being the scraped (most fragile)
    # source. Each stage only ever writes keys that are still None.
    return _backfill_from_finviz(ticker, _fill_market_cap(ticker, result))


def _fill_market_cap(ticker: str, out: dict) -> dict:
    """Resolve ``out["market_cap"]`` down the fallback chain; record the source.

    A value the providers supply is remembered on disk, so a later scan can
    still pass the size floor while every source is rate-limited. FMP is asked
    only after the remembered value misses, which keeps its 250-call free tier
    for tickers we have never seen. Never raises.
    """
    try:
        if market_cap._valid(out.get("market_cap")) is not None:
            out.setdefault("market_cap_source", "ticker_info")
            market_cap.remember(ticker, out["market_cap"], out["market_cap_source"])
            return out

        known = market_cap.cached(ticker)
        if known is not None:
            out["market_cap"] = known.value
            out["market_cap_source"] = f"last_known:{known.source}"
            out["market_cap_age_hours"] = known.age_hours
            return out

        for key, value in _fetch_fmp_fundamentals(ticker).items():
            if value is not None and out.get(key) is None:
                out[key] = value
        if out.get("market_cap") is not None:
            out["market_cap_source"] = "fmp"
            market_cap.remember(ticker, out["market_cap"], "fmp")
            return out

        estimate = market_cap.sec_market_cap(ticker, out.get("price"))
        if estimate is not None:
            out["market_cap"] = estimate
            out["market_cap_source"] = "sec_shares_x_price"
            market_cap.remember(ticker, estimate, "sec_shares_x_price")
    except Exception as exc:  # a fallback must never take the fetch down
        log.debug(f"{ticker}: market-cap fallback chain failed ({exc})")
    return out


def _backfill_from_finviz(ticker: str, out: dict) -> dict:
    """Fill fields the primary source left empty using the Finviz snapshot.

    Backfill only: an existing value is never overwritten, so enabling Finviz can
    add coverage but cannot silently change a number the primary source already
    supplied. No-op unless `finviz.enabled`, and any failure leaves ``out``
    untouched — fundamentals feed the composite score, so this must never be able
    to break scoring.
    """
    if not config.FINVIZ_ENABLED:
        return out
    try:
        from src.data.providers.finviz import fetch_fundamentals as _finviz_fundamentals

        snapshot = _finviz_fundamentals(ticker)
        if not snapshot:
            return out
        filled = [
            key for key, value in snapshot.items()
            if key in out and out.get(key) is None and value is not None
        ]
        for key in filled:
            out[key] = snapshot[key]
        if filled:
            out["_finviz_backfilled"] = filled
    except Exception as exc:  # never break scoring on a supplementary source
        log.debug(f"{ticker}: Finviz backfill unavailable ({exc})")
    return out


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
