"""Insider activity via SEC EDGAR Form 4 filings (no API key required)."""

import re
import time
from datetime import datetime, timedelta
from typing import List

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&forms=4"
_HEADERS = {"User-Agent": "StockBot research@example.com"}

EXECUTIVE_ROLES = {"ceo", "cfo", "president", "coo", "chairman", "director"}

_NEUTRAL = {
    "insider_score": 50,
    "net_insider_flow": 0,
    "buy_transactions": 0,
    "sell_transactions": 0,
    "flags": [],
    "signal": "NEUTRAL",
}


def analyze_insider_activity(ticker: str, days_back: int = 90) -> dict:
    """
    Parse SEC EDGAR Form 4 filings to detect insider buying/selling.

    Always returns a result (defaults to neutral if EDGAR is unavailable).
    """
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        transactions = _fetch_form4(ticker, start_date)
        return _score_transactions(transactions)
    except Exception as e:
        log.error(f"{ticker}: insider activity error: {e}")
        return _NEUTRAL.copy()


def _fetch_form4(ticker: str, start_date: str) -> List[dict]:
    """Fetch Form 4 filing data from SEC EDGAR full-text search."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start_date}&forms=4"

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug(f"EDGAR Form 4 fetch error: {e}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    transactions = []

    for hit in hits[:50]:  # Limit to 50 most recent
        src = hit.get("_source", {})
        transactions.append({
            "filed_at":        src.get("period_of_report", ""),
            "entity_name":     src.get("entity_name", ""),
            "transaction_type": src.get("transaction_type", ""),
            "shares":          _safe_float(src.get("transaction_shares")),
            "price":           _safe_float(src.get("transaction_price_per_share")),
            "role":            src.get("officer_title", "").lower(),
        })
        time.sleep(0.1)  # Be polite to EDGAR

    return transactions


def _score_transactions(transactions: List[dict]) -> dict:
    """Score insider activity and return structured result."""
    if not transactions:
        return _NEUTRAL.copy()

    buy_transactions  = [t for t in transactions if t.get("transaction_type") == "P"]  # Purchase
    sell_transactions = [t for t in transactions if t.get("transaction_type") == "S"]  # Sale

    insider_score = 50
    flags = []

    # CEO/CFO/President purchases are the strongest signal
    for t in buy_transactions:
        role  = t.get("role", "")
        price = t.get("price") or 0
        shares = t.get("shares") or 0
        value = price * shares

        if any(r in role for r in EXECUTIVE_ROLES):
            if value > 500_000:
                insider_score += 30
                flags.append(f"🔥 Executive bought ${value:,.0f}")
            elif value > 100_000:
                insider_score += 15
                flags.append(f"Executive bought ${value:,.0f}")

    # Multiple insiders buying is a strong signal
    if len(buy_transactions) >= 3:
        insider_score += 20
        flags.append(f"Multiple insiders buying ({len(buy_transactions)} transactions) ✅")
    elif len(buy_transactions) >= 1:
        insider_score += 10
        flags.append(f"Insider purchase detected ✅")

    # Large sell volume is mildly negative (could be diversification)
    if len(sell_transactions) >= 5:
        insider_score -= 10
        flags.append(f"Multiple insider sales ({len(sell_transactions)} transactions) ⚠️")

    total_bought = sum(
        (t.get("shares") or 0) * (t.get("price") or 0) for t in buy_transactions
    )
    total_sold = sum(
        (t.get("shares") or 0) * (t.get("price") or 0) for t in sell_transactions
    )
    net_flow = total_bought - total_sold

    insider_score = min(max(insider_score, 0), 100)

    signal = (
        "BULLISH" if net_flow > 0 and len(buy_transactions) > 0
        else "BEARISH" if net_flow < 0 and len(sell_transactions) > len(buy_transactions)
        else "NEUTRAL"
    )

    return {
        "insider_score":     insider_score,
        "net_insider_flow":  round(net_flow, 2),
        "buy_transactions":  len(buy_transactions),
        "sell_transactions": len(sell_transactions),
        "flags":             flags,
        "signal":            signal,
    }


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
