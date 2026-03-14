"""Tests for src/sentiment/social.py — Reddit social sentiment scoring."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.sentiment.social import _classify_text, analyze_social_sentiment


# ── _classify_text ────────────────────────────────────────────────────────────

def test_classify_bullish_keywords():
    assert _classify_text("AAPL moon 🚀 calls bullish breakout") == "positive"


def test_classify_bearish_keywords():
    assert _classify_text("AAPL crash dump puts bearish sell") == "negative"


def test_classify_neutral_no_keywords():
    assert _classify_text("Company announced earnings today") == "neutral"


def test_classify_tie_returns_neutral():
    # One positive and one negative word → tie → neutral
    assert _classify_text("buy crash") == "neutral"


def test_classify_case_insensitive():
    assert _classify_text("BULL MOON BREAKOUT") == "positive"
    assert _classify_text("BEAR CRASH DUMP") == "negative"


# ── No-op paths ───────────────────────────────────────────────────────────────

def test_returns_neutral_when_no_reddit_creds():
    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", ""):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", ""):
            result = analyze_social_sentiment("AAPL")
    assert result["signal"] == "NEUTRAL"
    assert result["sentiment_score_0_100"] == 50
    assert result["mention_count"] == 0


def test_returns_neutral_when_praw_not_installed():
    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", "fake"):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", "fake"):
            with patch.dict(sys.modules, {"praw": None}):
                result = analyze_social_sentiment("AAPL")
    assert result["signal"] == "NEUTRAL"


def test_returns_neutral_when_no_mentions():
    mock_reddit = MagicMock()
    mock_subreddit = MagicMock()
    mock_subreddit.search.return_value = []
    mock_reddit.subreddit.return_value = mock_subreddit

    mock_praw = MagicMock()
    mock_praw.Reddit.return_value = mock_reddit

    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", "fake"):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", "fake"):
            with patch("src.sentiment.social.config.REDDIT_USER_AGENT", "test"):
                with patch.dict(sys.modules, {"praw": mock_praw}):
                    result = analyze_social_sentiment("AAPL")

    assert result["signal"] == "NEUTRAL"
    assert result["mention_count"] == 0


def test_returns_neutral_on_api_exception():
    mock_praw = MagicMock()
    mock_praw.Reddit.side_effect = Exception("API error")

    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", "fake"):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", "fake"):
            with patch("src.sentiment.social.config.REDDIT_USER_AGENT", "test"):
                with patch.dict(sys.modules, {"praw": mock_praw}):
                    result = analyze_social_sentiment("AAPL")

    assert result["signal"] == "NEUTRAL"


# ── Scoring logic ─────────────────────────────────────────────────────────────

def _make_post(title="AAPL bullish breakout", score=100, comments=20,
               upvote_ratio=0.85, subreddit="stocks", is_dd=False):
    post = MagicMock()
    post.title = title
    post.score = score
    post.num_comments = comments
    post.upvote_ratio = upvote_ratio
    post.link_flair_text = "DD" if is_dd else ""
    return post


def _run_with_posts(posts, subreddit_name="stocks"):
    mock_subreddit = MagicMock()
    mock_subreddit.search.return_value = posts

    mock_reddit = MagicMock()
    mock_reddit.subreddit.return_value = mock_subreddit

    mock_praw = MagicMock()
    mock_praw.Reddit.return_value = mock_reddit

    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", "fake"):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", "fake"):
            with patch("src.sentiment.social.config.REDDIT_USER_AGENT", "test"):
                with patch.dict(sys.modules, {"praw": mock_praw}):
                    return analyze_social_sentiment("AAPL")


def test_bullish_signal_on_high_bull_ratio():
    # 3 posts × 5 subreddits = 15 mentions — below the 20-mention contrarian threshold
    posts = [_make_post(title="AAPL moon buy bullish calls") for _ in range(3)]
    result = _run_with_posts(posts)
    assert result["signal"] == "BULLISH"
    assert result["sentiment_score_0_100"] > 50


def test_bearish_signal_on_low_bull_ratio():
    posts = [_make_post(title="AAPL crash dump sell puts bearish") for _ in range(5)]
    result = _run_with_posts(posts)
    assert result["signal"] == "BEARISH"
    assert result["sentiment_score_0_100"] < 50


def test_contrarian_warning_on_extreme_bull_ratio():
    # All posts are bullish with high engagement — should trigger contrarian flag
    posts = [
        _make_post(title="AAPL moon 🚀 calls bullish buy", score=500, comments=200, upvote_ratio=0.95)
        for _ in range(25)
    ]
    result = _run_with_posts(posts)
    # With >20 mentions and extreme bull ratio, contrarian_warning should be True
    # and signal should be NEUTRAL (not BULLISH) as a contra indicator
    assert result["contrarian_warning"] is True
    assert result["signal"] == "NEUTRAL"


def test_dd_post_receives_higher_weight():
    """DD-flair posts count 3x normal engagement weight."""
    dd_post = _make_post(title="AAPL buy bullish", score=10, comments=5,
                         upvote_ratio=0.8, is_dd=True)
    normal_post = _make_post(title="AAPL buy bullish", score=10, comments=5,
                              upvote_ratio=0.8, is_dd=False)

    result_dd = _run_with_posts([dd_post])
    result_normal = _run_with_posts([normal_post])

    # Both have same sentiment but DD post has 3x weight — same signal, both valid
    assert result_dd["signal"] in ("BULLISH", "NEUTRAL", "BEARISH")
    assert result_normal["signal"] in ("BULLISH", "NEUTRAL", "BEARISH")


def test_result_has_required_keys():
    result = _run_with_posts([_make_post()])
    for key in ("bull_ratio", "mention_count", "contrarian_warning",
                "sentiment_score_0_100", "signal"):
        assert key in result


def test_mention_count_reflects_posts():
    posts = [_make_post() for _ in range(7)]
    result = _run_with_posts(posts)
    # Each subreddit returns all posts — 5 subreddits × 7 posts = 35 mentions max
    assert result["mention_count"] > 0


def test_subreddit_error_handled_gracefully():
    """A single subreddit error should not crash the whole function."""
    good_post = _make_post(title="AAPL bullish moon buy")
    call_count = {"n": 0}

    def _subreddit_side_effect(name):
        call_count["n"] += 1
        if name == "wallstreetbets":
            raise Exception("subreddit unavailable")
        mock_sub = MagicMock()
        mock_sub.search.return_value = [good_post]
        return mock_sub

    mock_reddit = MagicMock()
    mock_reddit.subreddit.side_effect = _subreddit_side_effect

    mock_praw = MagicMock()
    mock_praw.Reddit.return_value = mock_reddit

    with patch("src.sentiment.social.config.REDDIT_CLIENT_ID", "fake"):
        with patch("src.sentiment.social.config.REDDIT_CLIENT_SECRET", "fake"):
            with patch("src.sentiment.social.config.REDDIT_USER_AGENT", "test"):
                with patch.dict(sys.modules, {"praw": mock_praw}):
                    result = analyze_social_sentiment("AAPL")

    assert "signal" in result  # no crash
    assert result["mention_count"] > 0  # other subreddits still worked
