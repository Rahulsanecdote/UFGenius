"""The market-cap fallback chain behind UNKNOWN_MARKET_CAP.

On the Render paper trader's first scheduled scan every candidate was rejected
as UNKNOWN_MARKET_CAP: with Alpaca keys set, ticker info is Alpaca's asset
record, which has no market cap, and the only fallbacks (FMP, Finviz) were
keyless-off. These tests pin the chain that replaces "reject everything":
provider → last known value → FMP → SEC shares × price.

SEC payload fixtures are trimmed from real companyconcept responses fetched
2026-10-02 (Apple: dei cover-page shares; Alphabet: the dei concept 404s, the
us-gaap diluted total answers).
"""

from __future__ import annotations

import pytest

import src.utils.config as cfg
from src.fundamental import market_cap as mc
from src.fundamental.fetcher import fetch_fundamentals

AAPL_DEI = {"units": {"shares": [
    {"end": "2026-01-16", "val": 14681140000, "accn": "a1", "fy": 2026, "fp": "Q1",
     "form": "10-Q", "filed": "2026-01-30"},
    {"end": "2026-07-17", "val": 14594180000, "accn": "a3", "fy": 2026, "fp": "Q3",
     "form": "10-Q", "filed": "2026-07-31"},
    {"end": "2026-04-17", "val": 14687356000, "accn": "a2", "fy": 2026, "fp": "Q2",
     "form": "10-Q", "filed": "2026-05-01"},
]}}
GOOGL_DILUTED = {"units": {"shares": [
    {"start": "2026-01-01", "end": "2026-03-31", "val": 12238000000, "filed": "2026-04-30"},
    {"start": "2026-01-01", "end": "2026-06-30", "val": 12274000000, "filed": "2026-07-23"},
    {"start": "2026-04-01", "end": "2026-06-30", "val": 12309000000, "filed": "2026-07-23"},
    {"start": "2025-01-01", "end": "2025-12-31", "val": 12230000000, "filed": "2026-02-05"},
]}}
TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
           "1": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
           "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway"},
           "3": {"cik_str": 1800, "ticker": "ABBV", "title": "AbbVie"}}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class _SEC:
    """Answers like SEC EDGAR; records every URL asked for."""

    def __init__(self, concepts=None, fail=False):
        self.urls: list[str] = []
        self.concepts = concepts or {}
        self.fail = fail

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        assert headers and headers.get("User-Agent"), "SEC requires a User-Agent"
        if self.fail:
            raise ConnectionError("boom")
        if url.endswith("company_tickers.json"):
            return _Resp(200, TICKERS)
        for key, body in self.concepts.items():
            if key in url:
                return _Resp(200, body)
        return _Resp(404)


@pytest.fixture
def sec(monkeypatch):
    def _install(**kw):
        fake = _SEC(**kw)
        monkeypatch.setattr(cfg, "SEC_MARKET_CAP_ENABLED", True)
        monkeypatch.setattr(mc, "get_retry_session", lambda: fake)
        monkeypatch.setattr(mc, "_MIN_INTERVAL_SEC", 0.0)
        return fake
    return _install


# ── picking the share count ──────────────────────────────────────────────────

def test_latest_cover_page_count_wins():
    assert mc._latest_shares(AAPL_DEI) == 14594180000


def test_at_the_same_end_the_quarter_beats_the_year_to_date_average():
    assert mc._latest_shares(GOOGL_DILUTED) == 12309000000


@pytest.mark.parametrize("junk", [None, {}, {"units": {}}, {"units": {"shares": [{"val": "x"}]}}])
def test_junk_payloads_give_none(junk):
    assert mc._latest_shares(junk) is None


# ── SEC estimate ─────────────────────────────────────────────────────────────

def test_single_class_issuer_uses_cover_page_shares(sec):
    fake = sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    assert mc.sec_market_cap("AAPL", 250.0) == pytest.approx(14594180000 * 250.0)
    assert any("EntityCommonStockSharesOutstanding" in u for u in fake.urls)


def test_multi_class_issuer_falls_through_to_the_diluted_total(sec):
    """Alphabet's dei concept 404s — per-class facts are dimensional."""
    fake = sec(concepts={"CIK0001652044/us-gaap/WeightedAverage": GOOGL_DILUTED})
    assert mc.sec_market_cap("GOOGL", 200.0) == pytest.approx(12309000000 * 200.0)
    assert sum("companyconcept" in u for u in fake.urls) == 2


def test_alpacas_dotted_class_symbol_finds_secs_hyphenated_one(sec):
    sec(concepts={"CIK0001067983/dei/": AAPL_DEI})
    assert mc.sec_market_cap("BRK.B", 1.0) is not None


def test_share_counts_and_the_cik_map_are_cached(sec):
    fake = sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    mc.sec_market_cap("AAPL", 250.0)
    n = len(fake.urls)
    mc.sec_market_cap("AAPL", 260.0)          # new price, same shares
    assert len(fake.urls) == n


def test_a_ticker_sec_cannot_answer_is_not_asked_again_today(sec):
    fake = sec(concepts={})
    assert mc.sec_market_cap("ABBV", 190.0) is None
    n = len(fake.urls)
    assert mc.sec_market_cap("ABBV", 190.0) is None
    assert len(fake.urls) == n


def test_a_failing_sec_backs_off_instead_of_retrying_every_ticker(sec):
    fake = sec(fail=True)
    assert mc.sec_market_cap("AAPL", 250.0) is None
    n = len(fake.urls)
    assert mc.sec_market_cap("GOOGL", 200.0) is None
    assert len(fake.urls) == n


def test_switched_off_or_no_price_means_no_request(sec, monkeypatch):
    fake = sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    assert mc.sec_market_cap("AAPL", None) is None
    monkeypatch.setattr(cfg, "SEC_MARKET_CAP_ENABLED", False)
    assert mc.sec_market_cap("AAPL", 250.0) is None
    assert fake.urls == []


# ── the chain, through fetch_fundamentals ────────────────────────────────────

# What fetch_ticker_info returns with Alpaca keys: an asset record and a price,
# and no market cap — the shape that rejected every candidate on Render.
ALPACA_INFO = {"symbol": "AAPL", "longName": "Apple Inc.", "currentPrice": 250.0,
               "previousClose": 248.0, "status": "active"}


def test_the_render_failure_is_answered_by_sec(sec):
    sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    f = fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    assert f["market_cap"] == pytest.approx(14594180000 * 250.0)
    assert f["market_cap_source"] == "sec_shares_x_price"


def test_an_answer_is_remembered_for_when_every_source_is_down(sec, monkeypatch):
    sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    first = fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    # Now SEC fails and there is no FMP key: the remembered value still answers.
    sec(fail=True)
    second = fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    assert second["market_cap"] == first["market_cap"]
    assert second["market_cap_source"] == "last_known:sec_shares_x_price"


def test_a_provider_value_is_remembered_and_beats_every_fallback(sec):
    fake = sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    f = fetch_fundamentals("AAPL", info={**ALPACA_INFO, "marketCap": 3.7e12})
    assert f["market_cap"] == 3.7e12 and f["market_cap_source"] == "ticker_info"
    assert fake.urls == []
    later = fetch_fundamentals("AAPL", info={})            # provider gave nothing
    assert later["market_cap"] == 3.7e12
    assert later["market_cap_source"] == "last_known:ticker_info"


def test_fmp_is_asked_before_sec_but_not_after_a_remembered_value(sec, monkeypatch):
    fake = sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    calls = []
    import src.fundamental.fetcher as fetcher
    monkeypatch.setattr(fetcher, "_fetch_fmp_fundamentals",
                        lambda t: calls.append(t) or {"market_cap": 3.6e12})
    first = fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    assert first["market_cap_source"] == "fmp" and fake.urls == []
    fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    assert calls == ["AAPL"]                  # the 250/day free tier is spent once


def test_unknown_stays_unknown_when_nothing_can_answer(sec):
    """The filter must still fail closed when no source has a number."""
    sec(fail=True)
    f = fetch_fundamentals("ZZZZ", info={"symbol": "ZZZZ", "currentPrice": 5.0})
    assert f["market_cap"] is None
    assert "market_cap_source" not in f


def test_the_disqualifier_no_longer_rejects_a_real_large_cap(sec):
    """End to end through the real filter: the reason Render rejected AAPL is gone,
    and the floor is still enforced from the same number."""
    import numpy as np
    import pandas as pd
    from src.signals.filters import run_disqualification_filters
    idx = pd.date_range("2026-01-01", periods=60, freq="B")
    df = pd.DataFrame({"Open": 250.0, "High": 252.0, "Low": 248.0,
                       "Close": np.linspace(240, 250, 60), "Volume": 5e6}, index=idx)
    sec(concepts={"CIK0000320193/dei/": AAPL_DEI})
    f = fetch_fundamentals("AAPL", info=dict(ALPACA_INFO))
    reasons = run_disqualification_filters("AAPL", df, {"market_cap": None}, f)
    assert not any(r.startswith("UNKNOWN_MARKET_CAP") for r in reasons), reasons
    assert not any(r.startswith("MICRO_CAP") for r in reasons), reasons

    sec(fail=True)
    g = fetch_fundamentals("ZZZZ", info={"symbol": "ZZZZ", "currentPrice": 5.0})
    reasons = run_disqualification_filters("ZZZZ", df, {"market_cap": None}, g)
    assert any(r.startswith("UNKNOWN_MARKET_CAP") for r in reasons)
