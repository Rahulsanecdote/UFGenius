"""Fundamental scoring: Piotroski F-Score, Altman Z-Score, valuation composite."""

from src.fundamental.fetcher import fetch_fundamentals
from src.utils.logger import get_logger

log = get_logger(__name__)


def calculate_fundamental_score(ticker: str) -> dict:
    """
    Compute composite fundamental score (0-100) for a ticker.

    Weights:
        Piotroski F-Score:   25%
        Altman Z-Score:      20%
        Valuation (PEG):     25%
        Growth:              30%
    """
    fd = fetch_fundamentals(ticker)

    f_score, f_breakdown = _piotroski(fd)
    z_score = _altman_z(fd)
    valuation = _valuation_metrics(fd)
    growth    = _growth_metrics(fd)

    composite = _composite(f_score, z_score, valuation, growth)

    return {
        "ticker":            ticker,
        "piotroski_f_score": f_score,
        "piotroski_detail":  f_breakdown,
        "altman_z_score":    z_score,
        "valuation":         valuation,
        "growth":            growth,
        "fundamental_score": composite,
    }


def _piotroski(fd: dict) -> tuple:
    """Return (f_score 0-9, breakdown dict)."""
    score = 0
    detail = {}

    def _safe_div(a, b):
        try:
            if a is None or b is None or b == 0:
                return None
            return a / b
        except Exception:
            return None

    # F1: ROA > 0
    roa = _safe_div(fd.get("net_income"), fd.get("total_assets"))
    if roa is not None and roa > 0:
        score += 1
        detail["F1_roa_positive"] = True
    else:
        detail["F1_roa_positive"] = False

    # F2: Operating Cash Flow > 0
    ocf = fd.get("operating_cash_flow")
    if ocf is not None and ocf > 0:
        score += 1
        detail["F2_ocf_positive"] = True
    else:
        detail["F2_ocf_positive"] = False

    # F3: ROA improving YoY (skip if prior-year data unavailable)
    roa_prev = _safe_div(fd.get("net_income_prev"), fd.get("total_assets_prev"))
    if roa is not None and roa_prev is not None and roa > roa_prev:
        score += 1
        detail["F3_roa_improving"] = True
    else:
        detail["F3_roa_improving"] = None  # Data unavailable

    # F4: Accruals < 0 (cash earnings > reported earnings)
    accruals = _safe_div(
        (fd.get("net_income") or 0) - (ocf or 0),
        fd.get("total_assets")
    )
    if accruals is not None and accruals < 0:
        score += 1
        detail["F4_low_accruals"] = True
    else:
        detail["F4_low_accruals"] = False

    # F5: Debt decreased (long-term debt ratio)
    total_debt = fd.get("total_debt")
    total_assets = fd.get("total_assets")
    if total_debt is not None and total_assets and total_assets > 0:
        debt_ratio = total_debt / total_assets
        if debt_ratio < 0.5:
            score += 1
            detail["F5_low_leverage"] = True
        else:
            detail["F5_low_leverage"] = False
    else:
        detail["F5_low_leverage"] = None

    # F6: Current Ratio > 1.0
    curr_assets = fd.get("current_assets")
    curr_liabs  = fd.get("current_liabilities")
    if curr_assets is not None and curr_liabs and curr_liabs > 0:
        cr = curr_assets / curr_liabs
        if cr > 1.0:
            score += 1
            detail["F6_current_ratio_ok"] = True
        else:
            detail["F6_current_ratio_ok"] = False
    else:
        detail["F6_current_ratio_ok"] = None

    # F7: No excessive share dilution (skip — data not easily available)
    detail["F7_no_dilution"] = None

    # F8: Gross Margin > 0
    gm = _safe_div(fd.get("gross_profit"), fd.get("revenue"))
    if gm is not None and gm > 0:
        score += 1
        detail["F8_gross_margin_positive"] = True
    else:
        detail["F8_gross_margin_positive"] = False

    # F9: Asset Turnover > 0 (revenue / assets)
    at = _safe_div(fd.get("revenue"), fd.get("total_assets"))
    if at is not None and at > 0:
        score += 1
        detail["F9_asset_turnover_positive"] = True
    else:
        detail["F9_asset_turnover_positive"] = False

    return score, detail


def _altman_z(fd: dict) -> float | None:
    """Compute Altman Z-Score. Returns None if data insufficient."""
    try:
        ta = fd.get("total_assets")
        if not ta or ta == 0:
            return None

        wc  = (fd.get("current_assets") or 0) - (fd.get("current_liabilities") or 0)
        re  = fd.get("retained_earnings") or 0
        ebit = fd.get("ebit") or 0
        mc  = fd.get("market_cap") or 0
        tl  = fd.get("total_liabilities") or fd.get("total_debt") or 1
        rev = fd.get("revenue") or 0

        x1 = wc  / ta
        x2 = re  / ta
        x3 = ebit / ta
        x4 = mc  / tl
        x5 = rev / ta

        return round(1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5, 3)
    except Exception as e:
        log.debug(f"Altman Z calc error: {e}")
        return None


def _valuation_metrics(fd: dict) -> dict:
    def _safe(a, b):
        try:
            return round(a / b, 2) if (a and b and b != 0) else None
        except Exception:
            return None

    return {
        "pe_ratio":  fd.get("pe_ratio"),
        "peg_ratio": fd.get("peg_ratio"),
        "ps_ratio":  fd.get("ps_ratio"),
        "pb_ratio":  fd.get("pb_ratio"),
        "ev_ebitda": _safe(fd.get("enterprise_value"), fd.get("ebitda")),
        "fcf_yield": _safe(fd.get("free_cash_flow"), fd.get("market_cap")),
    }


def _growth_metrics(fd: dict) -> dict:
    def _safe_pct(v):
        return round(v * 100, 1) if v is not None else None

    rev  = fd.get("revenue")
    gp   = fd.get("gross_profit")
    ni   = fd.get("net_income")

    gm = round(gp / rev * 100, 1) if (gp and rev and rev != 0) else None
    nm = round(ni / rev * 100, 1) if (ni and rev and rev != 0) else None

    return {
        "revenue_growth_yoy_pct":  _safe_pct(fd.get("revenue_growth_yoy")),
        "earnings_growth_rate_pct": _safe_pct(fd.get("earnings_growth_rate")),
        "gross_margin_pct":        gm,
        "net_margin_pct":          nm,
    }


def _composite(f_score: int, z_score, valuation: dict, growth: dict) -> int:
    """
    Composite fundamental score (0-100).

    Weights:
        Piotroski (0-9 → 0-25 pts):   25%
        Z-Score safety (0-20 pts):     20%
        PEG valuation (0-25 pts):      25%
        Revenue growth (0-30 pts):     30%
    """
    score = 0.0

    # Piotroski
    score += (f_score / 9) * 25

    # Z-Score
    if z_score is not None:
        if z_score > 2.99:
            score += 20
        elif z_score > 1.81:
            score += 10
        # else: 0 — distress zone

    # PEG (lower = better growth at reasonable price)
    peg = valuation.get("peg_ratio")
    if peg is not None:
        if peg < 1.0:
            score += 25
        elif peg < 1.5:
            score += 15
        elif peg < 2.0:
            score += 8
        # Negative PEG (declining earnings) → 0

    # Revenue Growth
    rev_pct = growth.get("revenue_growth_yoy_pct")
    if rev_pct is not None:
        if rev_pct > 30:
            score += 30
        elif rev_pct > 15:
            score += 20
        elif rev_pct > 5:
            score += 10

    return min(int(score), 100)
