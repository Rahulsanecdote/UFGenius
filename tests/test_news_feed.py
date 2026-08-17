"""Tests for the news catalyst feed (src/catalysts/news_feed.py).

All offline: fetchers are monkeypatched; the classifier is exercised directly.
Covers the tier taxonomy and its precedence (dilution outranks everything),
the provider fallback order, both yfinance payload shapes, the age filter,
and the fail-soft contract (any failure ⇒ tier "none", never a raise).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.catalysts import news_feed
from src.catalysts.news_feed import (
    NewsHeadline,
    catalyst_news_for,
    classify_headlines,
    fetch_headlines,
)


def _h(title: str, provider: str = "test") -> NewsHeadline:
    return NewsHeadline(title=title, provider=provider)


# ── classifier taxonomy ──────────────────────────────────────────────────────

class TestClassifier:
    def test_strong_tiers(self):
        for title in [
            "Acme beats earnings estimates, raises guidance",
            "FDA approves Acme's lead drug",
            "Acme meets primary endpoint in phase 3 trial",
            "MegaCorp to acquire Acme for $2B",
            "Acme wins $500M defense contract",
            "Acme upgraded to Buy at Goldman",
            "Acme price target raised to $50",
        ]:
            assert classify_headlines([_h(title)])["tier"] == "strong", title

    def test_moderate_tiers(self):
        for title in [
            "Acme reports quarterly results",
            "Acme hosts investor day on Thursday",
            "Coverage initiated on Acme at Neutral",
            "Acme announces partnership with BigCo",
            "Acme to present at Jefferies conference",
        ]:
            assert classify_headlines([_h(title)])["tier"] == "moderate", title

    def test_weak_attention_churn(self):
        for title in [
            "Why is Acme stock soaring today?",
            "What's going on with Acme shares",
            "Acme sees unusual options activity",
            "5 trending stocks to watch this week",
        ]:
            assert classify_headlines([_h(title)])["tier"] == "weak", title

    def test_dilution_tier(self):
        for title in [
            "Acme announces $50M registered direct offering",
            "Acme prices public offering of common stock",
            "Acme announces reverse stock split",
            "Acme enters at-the-market program",
        ]:
            assert classify_headlines([_h(title)])["tier"] == "dilution", title

    def test_dilution_outranks_strong(self):
        # An offering headline IS the story, whatever else the wire says.
        result = classify_headlines([
            _h("Acme beats earnings estimates"),
            _h("Acme prices public offering of common stock"),
        ])
        assert result["tier"] == "dilution"

    def test_strong_outranks_moderate_and_weak(self):
        result = classify_headlines([
            _h("Why is Acme stock soaring today?"),
            _h("Acme hosts investor day"),
            _h("FDA approves Acme device"),
        ])
        assert result["tier"] == "strong"
        assert "FDA" in result["headline"]

    def test_no_match_is_none(self):
        result = classify_headlines([_h("Acme opens new office in Austin")])
        assert result == {"tier": "none", "headline": None, "provider": None}
        assert classify_headlines([])["tier"] == "none"

    def test_winning_headline_and_provider_are_reported(self):
        result = classify_headlines([_h("Acme wins $10M contract", provider="alpaca")])
        assert result["provider"] == "alpaca"
        assert "contract" in result["headline"]


# ── fetch fallback order ─────────────────────────────────────────────────────

class TestFetchFallback:
    def test_first_nonempty_provider_wins(self, monkeypatch):
        monkeypatch.setattr(news_feed, "_fetch_alpaca", lambda s, since: [])
        monkeypatch.setattr(
            news_feed, "_fetch_yfinance",
            lambda s, since: [_h("from yf", provider="yfinance")],
        )
        monkeypatch.setattr(
            news_feed, "_fetch_newsapi",
            lambda s, since: [_h("should not be reached", provider="newsapi")],
        )
        monkeypatch.setattr(
            news_feed, "_FETCHERS",
            (news_feed._fetch_alpaca, news_feed._fetch_yfinance, news_feed._fetch_newsapi),
        )
        items = fetch_headlines("ACME", use_cache=False)
        assert [h.provider for h in items] == ["yfinance"]

    def test_all_empty_yields_empty(self, monkeypatch):
        for name in ("_fetch_alpaca", "_fetch_yfinance", "_fetch_newsapi"):
            monkeypatch.setattr(news_feed, name, lambda s, since: [])
        monkeypatch.setattr(
            news_feed, "_FETCHERS",
            (news_feed._fetch_alpaca, news_feed._fetch_yfinance, news_feed._fetch_newsapi),
        )
        assert fetch_headlines("ACME", use_cache=False) == []

    def test_catalyst_news_for_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("provider exploded")
        monkeypatch.setattr(news_feed, "fetch_headlines", boom)
        assert catalyst_news_for("ACME")["tier"] == "none"


# ── yfinance payload shapes ──────────────────────────────────────────────────

class TestYfinanceShapes:
    def _with_news(self, monkeypatch, payload):
        class _T:
            news = payload
        import yfinance as yf
        monkeypatch.setattr(yf, "Ticker", lambda s: _T())

    def test_flat_shape(self, monkeypatch):
        now = datetime.now(timezone.utc)
        self._with_news(monkeypatch, [
            {"title": "Flat shape headline", "publisher": "Wire",
             "providerPublishTime": int(now.timestamp()), "link": "http://x"},
        ])
        items = news_feed._fetch_yfinance("ACME", now - timedelta(hours=36))
        assert len(items) == 1 and items[0].source == "Wire"

    def test_nested_content_shape(self, monkeypatch):
        now = datetime.now(timezone.utc)
        self._with_news(monkeypatch, [
            {"content": {"title": "Nested shape headline",
                         "pubDate": now.isoformat(),
                         "provider": {"displayName": "WireCo"},
                         "canonicalUrl": {"url": "http://y"}}},
        ])
        items = news_feed._fetch_yfinance("ACME", now - timedelta(hours=36))
        assert len(items) == 1 and items[0].source == "WireCo"

    def test_age_filter_drops_stale(self, monkeypatch):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)
        self._with_news(monkeypatch, [
            {"title": "Stale headline", "publisher": "Wire",
             "providerPublishTime": int(old.timestamp())},
        ])
        items = news_feed._fetch_yfinance("ACME", now - timedelta(hours=36))
        assert items == []
