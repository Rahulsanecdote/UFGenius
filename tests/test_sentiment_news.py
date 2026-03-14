"""Tests for src/sentiment/news.py — news sentiment scoring."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.sentiment.news import _extract_domain, analyze_news_sentiment


# ── No-op paths (no credentials / missing deps) ───────────────────────────────

def test_returns_neutral_when_no_api_key():
    with patch("src.sentiment.news.config.NEWSAPI_KEY", ""):
        result = analyze_news_sentiment("AAPL")
    assert result["signal"] == "NEUTRAL"
    assert result["sentiment_score_0_100"] == 50
    assert result["article_count"] == 0


def test_returns_neutral_when_newsapi_not_installed():
    with patch("src.sentiment.news.config.NEWSAPI_KEY", "fake_key"):
        with patch.dict(sys.modules, {"newsapi": None, "vaderSentiment": None, "vaderSentiment.vaderSentiment": None}):
            result = analyze_news_sentiment("AAPL")
    assert result["signal"] == "NEUTRAL"
    assert result["sentiment_score_0_100"] == 50


def test_returns_neutral_on_api_exception():
    mock_newsapi = MagicMock()
    mock_newsapi.NewsApiClient.side_effect = Exception("API error")

    mock_vader = MagicMock()

    with patch("src.sentiment.news.config.NEWSAPI_KEY", "fake_key"):
        with patch.dict(sys.modules, {
            "newsapi": mock_newsapi,
            "vaderSentiment": MagicMock(),
            "vaderSentiment.vaderSentiment": mock_vader,
        }):
            result = analyze_news_sentiment("AAPL")

    assert result["signal"] == "NEUTRAL"


def test_returns_neutral_when_no_articles():
    mock_client = MagicMock()
    mock_client.get_everything.return_value = {"articles": []}

    mock_newsapi_module = MagicMock()
    mock_newsapi_module.NewsApiClient.return_value = mock_client

    mock_vader_module = MagicMock()
    mock_vader_module.SentimentIntensityAnalyzer.return_value = MagicMock()

    with patch("src.sentiment.news.config.NEWSAPI_KEY", "fake_key"):
        with patch.dict(sys.modules, {
            "newsapi": mock_newsapi_module,
            "vaderSentiment": MagicMock(),
            "vaderSentiment.vaderSentiment": mock_vader_module,
        }):
            result = analyze_news_sentiment("AAPL")

    assert result["signal"] == "NEUTRAL"
    assert result["article_count"] == 0


# ── Scoring logic (pure functions) ───────────────────────────────────────────

def _recent_ts() -> str:
    """Return a publishedAt string from 1 hour ago so recency weight is near 1.0."""
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_article(title="Apple reports strong earnings", url="https://reuters.com/article",
                  published=None, description=""):
    if published is None:
        published = _recent_ts()
    return {"title": title, "description": description, "url": url, "publishedAt": published}


def _run_with_articles(articles, compound_score=0.5):
    """Helper: run analyze_news_sentiment with mocked NewsAPI + VADER responses."""
    mock_analyzer = MagicMock()
    mock_analyzer.polarity_scores.return_value = {"compound": compound_score}

    mock_client = MagicMock()
    mock_client.get_everything.return_value = {"articles": articles}

    mock_newsapi_mod = MagicMock()
    mock_newsapi_mod.NewsApiClient.return_value = mock_client

    mock_vader_mod = MagicMock()
    mock_vader_mod.SentimentIntensityAnalyzer.return_value = mock_analyzer

    with patch("src.sentiment.news.config.NEWSAPI_KEY", "fake_key"):
        with patch.dict(sys.modules, {
            "newsapi": mock_newsapi_mod,
            "vaderSentiment": MagicMock(),
            "vaderSentiment.vaderSentiment": mock_vader_mod,
        }):
            return analyze_news_sentiment("AAPL", "Apple Inc")


def test_bullish_signal_on_positive_composite():
    result = _run_with_articles([_make_article()], compound_score=0.5)
    assert result["signal"] == "BULLISH"
    assert result["sentiment_score_0_100"] > 50
    assert result["article_count"] == 1


def test_bearish_signal_on_negative_composite():
    result = _run_with_articles([_make_article()], compound_score=-0.5)
    assert result["signal"] == "BEARISH"
    assert result["sentiment_score_0_100"] < 50


def test_neutral_signal_on_near_zero_composite():
    result = _run_with_articles([_make_article()], compound_score=0.05)
    assert result["signal"] == "NEUTRAL"


def test_result_contains_required_keys():
    result = _run_with_articles([_make_article()], compound_score=0.3)
    for key in ("composite_score", "signal", "article_count", "articles", "sentiment_score_0_100"):
        assert key in result


def test_articles_capped_at_five_in_result():
    articles = [_make_article(title=f"Article {i}") for i in range(20)]
    result = _run_with_articles(articles, compound_score=0.4)
    assert len(result["articles"]) <= 5


def test_malformed_timestamp_falls_back_to_24h():
    """Malformed publishedAt should log debug and use 24h age (not crash)."""
    article = _make_article(published="not-a-date", title="AAPL bullish buy")
    result = _run_with_articles([article], compound_score=0.3)
    # Result should still come through, no exception
    assert "signal" in result


def test_empty_title_and_description_article_skipped():
    articles = [{"title": None, "description": None, "url": "", "publishedAt": "2024-01-15T10:00:00Z"}]
    result = _run_with_articles(articles, compound_score=0.5)
    # Empty text articles are skipped → neutral
    assert result["signal"] == "NEUTRAL"
    assert result["article_count"] == 0


# ── Source authority weighting ────────────────────────────────────────────────

def test_high_authority_source_receives_higher_weight():
    """Reuters (1.5) and unknown domain (0.8) should produce different weightings."""
    reuters_article = _make_article(url="https://reuters.com/article/x")
    unknown_article = _make_article(url="https://someblog.io/post")

    result_reuters = _run_with_articles([reuters_article], compound_score=0.5)
    result_unknown = _run_with_articles([unknown_article], compound_score=0.5)

    # Both bullish, but Reuters composite higher due to weight 1.5 vs 0.8
    assert result_reuters["composite_score"] > result_unknown["composite_score"]


# ── _extract_domain ───────────────────────────────────────────────────────────

def test_extract_domain_strips_www():
    assert _extract_domain("https://www.reuters.com/article") == "reuters.com"


def test_extract_domain_no_www():
    assert _extract_domain("https://bloomberg.com/news/x") == "bloomberg.com"


def test_extract_domain_invalid_url():
    assert _extract_domain("not-a-url") == ""


def test_extract_domain_empty_string():
    assert _extract_domain("") == ""
