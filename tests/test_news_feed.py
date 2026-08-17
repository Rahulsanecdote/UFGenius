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
        monkeypatch.setattr(news_feed, "_fetch_alpaca", lambda s, since, c="": [])
        monkeypatch.setattr(
            news_feed, "_fetch_yfinance",
            lambda s, since, c="": [_h("from yf", provider="yfinance")],
        )
        monkeypatch.setattr(
            news_feed, "_fetch_newsapi",
            lambda s, since, c="": [_h("should not be reached", provider="newsapi")],
        )
        monkeypatch.setattr(
            news_feed, "_FETCHERS",
            (news_feed._fetch_alpaca, news_feed._fetch_yfinance, news_feed._fetch_newsapi),
        )
        items = fetch_headlines("ACME", use_cache=False)
        assert [h.provider for h in items] == ["yfinance"]

    def test_all_empty_yields_empty(self, monkeypatch):
        for name in ("_fetch_alpaca", "_fetch_yfinance", "_fetch_newsapi"):
            monkeypatch.setattr(news_feed, name, lambda s, since, c="": [])
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


# ── negation guard (Codex P1) ────────────────────────────────────────────────

class TestNegationGuard:
    def test_adverse_endpoint_headlines_are_not_strong(self):
        for title in [
            "Acme trial fails to meet its primary endpoint",
            "Acme did not meet the primary endpoint in phase 3",
            "Acme misses on earnings estimates",
            "FDA rejects Acme's application",
        ]:
            assert classify_headlines([_h(title)])["tier"] != "strong", title

    def test_positive_forms_still_classify_strong(self):
        assert classify_headlines([_h("Acme meets primary endpoint")])["tier"] == "strong"
        assert classify_headlines([_h("Acme beats earnings estimates")])["tier"] == "strong"

    def test_negated_strong_falls_through_to_lower_tiers(self):
        # Adverse headline + a genuine moderate headline → moderate wins.
        result = classify_headlines([
            _h("Acme trial fails to meet its primary endpoint"),
            _h("Acme hosts investor day"),
        ])
        assert result["tier"] == "moderate"


# ── NewsAPI identity + cutoff (Codex P2s) ────────────────────────────────────

class TestNewsapiGuards:
    def test_identity_requires_symbol_token_or_company(self):
        from src.catalysts.news_feed import _newsapi_identity_ok
        assert _newsapi_identity_ok("AI stocks rally as CAT reports", "CAT", "") is True
        assert _newsapi_identity_ok("The cat sat on the mat", "CAT", "") is False
        assert _newsapi_identity_ok("Caterpillar wins contract", "CAT", "Caterpillar") is True
        # case-sensitive token: lowercase 'ai' in prose is not the ticker AI
        assert _newsapi_identity_ok("ai is transforming industry", "AI", "") is False

    def test_short_symbol_without_company_skips_newsapi(self, monkeypatch):
        monkeypatch.setattr(news_feed.config, "NEWSAPI_KEY", "k")
        out = news_feed._fetch_newsapi("AI", datetime.now(timezone.utc))
        assert out == []

    def test_newsapi_applies_precise_time_cutoff_and_identity(self, monkeypatch):
        import sys, types
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=6)
        articles = [
            {"title": "ACME beats earnings estimates", "source": {"name": "Wire"},
             "url": "u", "publishedAt": now.isoformat()},
            {"title": "ACME wins contract",  # same day but BEFORE the cutoff
             "source": {"name": "Wire"}, "url": "u",
             "publishedAt": (since - timedelta(hours=3)).isoformat()},
            {"title": "unrelated acme-lowercase prose", "source": {"name": "Wire"},
             "url": "u", "publishedAt": now.isoformat()},
        ]
        fake = types.ModuleType("newsapi")
        class NewsApiClient:  # noqa: N801 - mimics the real class name
            def __init__(self, api_key):
                pass
            def get_everything(self, **kw):
                return {"articles": articles}
        fake.NewsApiClient = NewsApiClient
        monkeypatch.setitem(sys.modules, "newsapi", fake)
        monkeypatch.setattr(news_feed.config, "NEWSAPI_KEY", "k")
        out = news_feed._fetch_newsapi("ACME", since)
        assert [h.title for h in out] == ["ACME beats earnings estimates"]


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
