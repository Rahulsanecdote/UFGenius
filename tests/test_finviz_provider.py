"""Tests for the Finviz fundamentals/screener provider.

Entirely offline: HTML fixtures stand in for the live site, so the parser is
exercised without a request. That matters more than usual here — the provider
reads scraped markup, so the failure mode to guard is "layout changed, values
mis-parsed", not "network down".

The behaviours pinned below are the ones that keep a scraped source safe to
depend on: off unless enabled, missing values stay missing (never 0.0), tables
matched by content, and every failure path degrades to None/[] instead of
raising into a caller.
"""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.data.providers import finviz


def _snapshot_html(**overrides: str) -> str:
    """A Finviz-shaped snapshot page: alternating label/value cells."""
    values = {
        "Market Cap": "3013.45B", "Income": "99.80B", "Sales": "391.03B",
        "Price": "302.25", "P/E": "31.42", "PEG": "2.15", "P/S": "7.71",
        "P/B": "51.20", "EPS (ttm)": "6.42", "Book/sh": "5.90",
        "Shs Outstand": "14.94B", "Sales Y/Y TTM": "6.10%",
        "EPS Y/Y TTM": "12.30%", "EPS next Y": "8.40%",
        "ROA": "27.50%", "ROE": "160.10%", "Debt/Eq": "1.87",
        "Current Ratio": "0.87", "Gross Margin": "46.20%",
        "Insider Own": "0.10%", "Short Float": "0.65%", "Beta": "1.24",
        "Avg Volume": "54.32M", "Dividend %": "0.44%", "Earnings": "Oct 30 AMC",
    }
    values.update(overrides)
    cells = "".join(
        f"<td>{label}</td><td>{value}</td>" for label, value in values.items()
    )
    return f"<html><body><table class='snapshot-table2'><tr>{cells}</tr></table></body></html>"


def _screener_html(tickers: list[str]) -> str:
    rows = "".join(
        f"<tr><td>{i+1}</td><td>{t}</td><td>Company {t}</td><td>Tech</td></tr>"
        for i, t in enumerate(tickers)
    )
    return (
        "<html><body><table>"
        "<tr><td>No.</td><td>Ticker</td><td>Company</td><td>Sector</td></tr>"
        f"{rows}</table></body></html>"
    )


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", True)
    monkeypatch.setattr(cfg, "FINVIZ_MIN_REQUEST_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(cfg, "FINVIZ_SCREENER_MAX_RESULTS", 200)
    monkeypatch.setattr(finviz, "_last_request_at", 0.0)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Isolate from the shared disk cache so tests never leak into each other."""
    monkeypatch.setattr(finviz.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(finviz.cache, "set", lambda *_a, **_k: None)


# ── number parsing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("3013.45B", 3013.45e9), ("54.32M", 54.32e6), ("1.5K", 1500.0),
    ("2.1T", 2.1e12), ("12.5%", 12.5), ("-3.25%", -3.25),
    ("1,234.5", 1234.5), ("302.25", 302.25), ("-12", -12.0),
])
def test_parses_finviz_number_formats(raw, expected):
    assert finviz._parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["-", "--", "N/A", "NA", "", "   ", None, "abc"])
def test_missing_values_are_none_not_zero(raw):
    """A missing fundamental must never read as a measured 0.0."""
    assert finviz._parse_number(raw) is None


# ── fundamentals ──────────────────────────────────────────────────────────────

def test_fetch_fundamentals_maps_canonical_keys(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    assert f["ticker"] == "AAPL" and f["source"] == "finviz"
    assert f["market_cap"] == pytest.approx(3013.45e9)
    assert f["net_income"] == pytest.approx(99.80e9)
    assert f["revenue"] == pytest.approx(391.03e9)
    assert f["eps"] == pytest.approx(6.42)
    assert f["pe_ratio"] == pytest.approx(31.42)
    assert f["pb_ratio"] == pytest.approx(51.20)
    assert f["book_value_per_share"] == pytest.approx(5.90)
    assert f["shares_outstanding"] == pytest.approx(14.94e9)
    # Decimal, not percent — see test_growth_fields_are_converted_to_canonical_decimals.
    assert f["revenue_growth_yoy"] == pytest.approx(0.0610)


def test_growth_fields_are_converted_to_canonical_decimals(monkeypatch):
    """Finviz prints '6.10%'; the canonical contract stores 0.061.

    fundamental/scorer.py::_growth_metrics multiplies by 100 on the way out
    (yfinance's revenueGrowth is already a decimal), so returning 6.1 here would
    reach the scorer as 610% and max out the 30-point growth component.
    """
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    assert f["revenue_growth_yoy"] == pytest.approx(0.0610)
    assert f["eps_growth_yoy"] == pytest.approx(0.1230)
    assert f["earnings_growth_rate"] == pytest.approx(0.0840)


def test_scorer_reads_finviz_growth_at_the_right_scale(monkeypatch):
    """End-to-end unit check against the consumer that actually multiplies."""
    from src.fundamental.scorer import _growth_metrics

    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    assert _growth_metrics(f)["revenue_growth_yoy_pct"] == pytest.approx(6.1)


def test_native_percentage_extras_keep_percent_units(monkeypatch):
    """The `_pct` extras are Finviz-native and never enter the canonical contract."""
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    assert f["gross_margin_pct"] == pytest.approx(46.20)
    assert f["roa"] == pytest.approx(27.50)


def test_fetch_fundamentals_keeps_finviz_native_extras(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    assert f["roe"] == pytest.approx(160.10)
    assert f["debt_to_equity"] == pytest.approx(1.87)
    assert f["short_float_pct"] == pytest.approx(0.65)
    assert f["earnings_date_raw"] == "Oct 30 AMC"  # kept as text, not parsed


def test_unpublished_fields_are_absent_not_fabricated(monkeypatch):
    """Finviz states no absolute debt or cash-flow lines — don't invent them."""
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html())
    f = finviz.fetch_fundamentals("AAPL")
    for absent in ("total_debt", "total_assets", "total_liabilities",
                   "operating_cash_flow", "free_cash_flow", "retained_earnings"):
        assert absent not in f


def test_parses_a_realistic_multi_row_grid(monkeypatch):
    """The live snapshot is a 12-column grid over many rows, not one long row.

    Each row is label,value,label,value..., so flattening must pair across row
    boundaries without dropping or shifting a cell.
    """
    pairs = [
        ("Market Cap", "3013.45B"), ("Income", "99.80B"), ("Sales", "391.03B"),
        ("Book/sh", "5.90"), ("P/E", "31.42"), ("EPS (ttm)", "6.42"),
        ("PEG", "2.15"), ("P/S", "7.71"), ("P/B", "51.20"),
        ("ROA", "27.50%"), ("ROE", "160.10%"), ("Debt/Eq", "1.87"),
        ("Shs Outstand", "14.94B"), ("Beta", "1.24"), ("Price", "302.25"),
        ("Current Ratio", "0.87"), ("Short Float", "0.65%"), ("Avg Volume", "54.32M"),
    ]
    rows = ""
    for i in range(0, len(pairs), 6):  # six label/value pairs per row = 12 cells
        cells = "".join(f"<td>{k}</td><td>{v}</td>" for k, v in pairs[i:i + 6])
        rows += f"<tr>{cells}</tr>"
    monkeypatch.setattr(
        finviz, "_throttled_get",
        lambda _u: f"<html><table class='snapshot-table2'>{rows}</table></html>",
    )
    f = finviz.fetch_fundamentals("AAPL")
    assert f["market_cap"] == pytest.approx(3013.45e9)
    assert f["pe_ratio"] == pytest.approx(31.42)      # first row
    assert f["roe"] == pytest.approx(160.10)          # middle row
    assert f["price"] == pytest.approx(302.25)        # last row
    assert f["avg_volume"] == pytest.approx(54.32e6)


def test_dashes_in_the_page_become_none(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: _snapshot_html(**{"PEG": "-"}))
    assert finviz.fetch_fundamentals("AAPL")["peg_ratio"] is None


# ── failing soft ──────────────────────────────────────────────────────────────

def test_disabled_provider_returns_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", False)
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: pytest.fail("must not request when disabled"))
    assert finviz.fetch_fundamentals("AAPL") is None
    assert finviz.screen("cap_large") == []


def test_layout_change_degrades_to_none(monkeypatch):
    """Unrecognisable markup yields None, not a half-parsed dict."""
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: "<html><body><p>nothing here</p></body></html>")
    assert finviz.fetch_fundamentals("AAPL") is None


def test_unrelated_table_is_not_mistaken_for_the_snapshot(monkeypatch):
    html = "<html><table><tr><td>Foo</td><td>1</td><td>Bar</td><td>2</td></tr></table></html>"
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: html)
    assert finviz.fetch_fundamentals("AAPL") is None


def test_fetch_failure_returns_none(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: None)
    assert finviz.fetch_fundamentals("AAPL") is None


def test_http_exception_never_escapes(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(finviz, "get_text", _boom)
    assert finviz.fetch_fundamentals("AAPL") is None
    assert finviz.screen("cap_large") == []


@pytest.mark.parametrize("bad", ["", "not a ticker", "../../etc/passwd", "A" * 20, "a b"])
def test_malformed_tickers_are_refused_before_any_request(monkeypatch, bad):
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: pytest.fail("must not request a malformed ticker"))
    assert finviz.fetch_fundamentals(bad) is None


# ── screener ──────────────────────────────────────────────────────────────────

def test_screen_extracts_tickers_by_header(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: _screener_html(["AAPL", "MSFT", "NVDA"]))
    assert finviz.screen("cap_large", max_results=3) == ["AAPL", "MSFT", "NVDA"]


def test_screen_paginates_and_dedupes(monkeypatch):
    pages = {1: [f"T{i:02d}" for i in range(20)], 21: [f"U{i:02d}" for i in range(20)]}

    def _get(url):
        offset = int(url.split("r=")[1].split("&")[0])
        return _screener_html(pages.get(offset, []))

    monkeypatch.setattr(finviz, "_throttled_get", _get)
    out = finviz.screen("cap_large", max_results=40)
    assert len(out) == 40 and len(set(out)) == 40
    assert out[0] == "T00" and out[-1] == "U19"


def test_screen_stops_on_a_repeated_page(monkeypatch):
    """A site that keeps serving page 1 must not loop forever."""
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: _screener_html(["AAPL", "MSFT"]))
    assert finviz.screen("cap_large", max_results=100) == ["AAPL", "MSFT"]


def test_screen_honours_max_results(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: _screener_html([f"T{i:02d}" for i in range(20)]))
    assert len(finviz.screen("cap_large", max_results=5)) == 5


def test_screen_with_zero_limit_makes_no_request(monkeypatch):
    monkeypatch.setattr(finviz, "_throttled_get",
                        lambda _u: pytest.fail("must not request with a zero limit"))
    assert finviz.screen("cap_large", max_results=0) == []


def test_screen_passes_filters_through_untouched(monkeypatch):
    seen: dict[str, str] = {}

    def _get(url):
        seen["url"] = url
        return _screener_html(["AAPL"])

    monkeypatch.setattr(finviz, "_throttled_get", _get)
    finviz.screen("cap_midover,fa_pe_u20", signal="ta_topgainers", max_results=1)
    assert "f=cap_midover,fa_pe_u20" in seen["url"]
    assert "s=ta_topgainers" in seen["url"]


def test_screener_junk_rows_are_rejected(monkeypatch):
    html = _screener_html(["AAPL", "not-a-ticker", "MSFT"])
    monkeypatch.setattr(finviz, "_throttled_get", lambda _u: html)
    assert finviz.screen("x", max_results=10) == ["AAPL", "MSFT"]


# ── politeness ────────────────────────────────────────────────────────────────

def test_requests_are_rate_limited(monkeypatch):
    monkeypatch.setattr(cfg, "FINVIZ_MIN_REQUEST_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(finviz, "get_text", lambda *_a, **_k: _snapshot_html())
    monkeypatch.setattr(finviz, "_last_request_at", 0.0)
    import time as _t

    start = _t.monotonic()
    for _ in range(3):
        finviz._throttled_get("https://example.invalid")
    assert _t.monotonic() - start >= 0.10  # at least two enforced gaps


def test_a_user_agent_is_always_sent():
    assert finviz._headers()["User-Agent"].strip()


# ── fundamentals backfill integration ─────────────────────────────────────────

def test_backfill_is_a_noop_when_disabled(monkeypatch):
    import src.fundamental.fetcher as ff

    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", False)
    out = {"market_cap": None, "pe_ratio": None}
    assert ff._backfill_from_finviz("AAPL", out) == {"market_cap": None, "pe_ratio": None}


def test_backfill_fills_only_missing_fields(monkeypatch):
    import src.fundamental.fetcher as ff

    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", True)
    monkeypatch.setattr(
        "src.data.providers.finviz.fetch_fundamentals",
        lambda _t: {"market_cap": 999.0, "pe_ratio": 12.0, "eps": 3.0},
    )
    out = {"market_cap": 111.0, "pe_ratio": None, "eps": None}
    got = ff._backfill_from_finviz("AAPL", out)
    assert got["market_cap"] == 111.0, "must never overwrite the primary source"
    assert got["pe_ratio"] == 12.0 and got["eps"] == 3.0
    assert sorted(got["_finviz_backfilled"]) == ["eps", "pe_ratio"]


def test_backfill_ignores_keys_outside_the_canonical_contract(monkeypatch):
    import src.fundamental.fetcher as ff

    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", True)
    monkeypatch.setattr(
        "src.data.providers.finviz.fetch_fundamentals",
        lambda _t: {"roe": 160.0, "short_float_pct": 0.65},
    )
    out = {"market_cap": None}
    got = ff._backfill_from_finviz("AAPL", out)
    assert "roe" not in got and "short_float_pct" not in got


def test_backfill_failure_leaves_fundamentals_untouched(monkeypatch):
    """Fundamentals feed the composite — a scraper fault must not break scoring."""
    import src.fundamental.fetcher as ff

    def _boom(_t):
        raise RuntimeError("finviz exploded")

    monkeypatch.setattr(cfg, "FINVIZ_ENABLED", True)
    monkeypatch.setattr("src.data.providers.finviz.fetch_fundamentals", _boom)
    out = {"market_cap": None, "pe_ratio": 12.0}
    assert ff._backfill_from_finviz("AAPL", out) == {"market_cap": None, "pe_ratio": 12.0}
