"""Social media sentiment via Reddit PRAW. Degrades gracefully without credentials."""

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options", "stockmarket"]

_NEUTRAL = {
    "bull_ratio": 0.5,
    "mention_count": 0,
    "contrarian_warning": False,
    "sentiment_score_0_100": 50,
    "signal": "NEUTRAL",
}

_POSITIVE_WORDS = {
    "buy", "bull", "bullish", "moon", "🚀", "calls", "long", "upside",
    "breakout", "growth", "profit", "gains", "rally", "squeeze",
}
_NEGATIVE_WORDS = {
    "sell", "bear", "bearish", "puts", "short", "crash", "dump", "bags",
    "overvalued", "bubble", "collapse", "decline", "loss",
}


def analyze_social_sentiment(ticker: str) -> dict:
    """
    Scan Reddit for ticker mentions and compute bull/bear ratio.

    Returns neutral baseline if Reddit credentials are not configured.
    """
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        log.debug(f"{ticker}: Reddit credentials not set — returning neutral social sentiment")
        return _NEUTRAL.copy()

    try:
        import praw
    except ImportError:
        log.warning("praw not installed — returning neutral social sentiment")
        return _NEUTRAL.copy()

    try:
        reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
        )

        mentions = []

        for sub_name in SUBREDDITS:
            try:
                subreddit = reddit.subreddit(sub_name)
                for post in subreddit.search(ticker, time_filter="day", limit=25):
                    flair = (post.link_flair_text or "").lower()
                    is_dd = "dd" in flair or "due diligence" in flair
                    mentions.append({
                        "title":        post.title,
                        "score":        post.score,
                        "comments":     post.num_comments,
                        "upvote_ratio": post.upvote_ratio,
                        "subreddit":    sub_name,
                        "is_dd":        is_dd,
                    })
            except Exception as e:
                log.debug(f"Subreddit {sub_name} error: {e}")
                continue

        if not mentions:
            return _NEUTRAL.copy()

        total_weight = 0.0
        bull_weight  = 0.0

        for m in mentions:
            engagement = max(m["score"] + m["comments"], 1) * m["upvote_ratio"]
            weight = engagement * (3 if m["is_dd"] else 1)
            sentiment = _classify_text(m["title"])
            if sentiment == "positive":
                bull_weight += weight
            total_weight += weight

        bull_ratio = bull_weight / total_weight if total_weight > 0 else 0.5
        contrarian = bool(bull_ratio > 0.85 and len(mentions) > 20)

        if contrarian:
            signal = "NEUTRAL"  # Too bullish = contrarian
        elif bull_ratio > 0.6:
            signal = "BULLISH"
        elif bull_ratio < 0.4:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        return {
            "bull_ratio":         round(bull_ratio, 3),
            "mention_count":      len(mentions),
            "contrarian_warning": contrarian,
            "sentiment_score_0_100": int(bull_ratio * 100),
            "signal": signal,
        }

    except Exception as e:
        log.error(f"{ticker}: social sentiment error: {e}")
        return _NEUTRAL.copy()


def _classify_text(text: str) -> str:
    """Simple bag-of-words sentiment for Reddit titles."""
    lower = text.lower()
    pos = sum(1 for w in _POSITIVE_WORDS if w in lower)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in lower)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"
