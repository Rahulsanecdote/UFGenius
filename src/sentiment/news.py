"""News sentiment via NewsAPI + VADER. Gracefully degrades if key is missing."""

import math
from datetime import datetime, timedelta
from urllib.parse import urlparse

from src.utils import config
from src.utils.logger import get_logger

log = get_logger(__name__)

SOURCE_WEIGHTS = {
    "reuters.com": 1.5,
    "apnews.com": 1.5,
    "bloomberg.com": 1.5,
    "cnbc.com": 1.3,
    "wsj.com": 1.3,
    "ft.com": 1.3,
    "seekingalpha.com": 1.0,
    "marketwatch.com": 1.0,
    "barrons.com": 1.2,
    "thestreet.com": 0.9,
}

_NEUTRAL = {
    "composite_score": 0.0,
    "signal": "NEUTRAL",
    "article_count": 0,
    "articles": [],
    "sentiment_score_0_100": 50,
}


def analyze_news_sentiment(ticker: str, company_name: str = "") -> dict:
    """
    Fetch and score recent news for a ticker.

    Returns neutral baseline if NEWSAPI_KEY is not set.
    """
    if not config.NEWSAPI_KEY:
        log.debug(f"{ticker}: NEWSAPI_KEY not set — returning neutral news sentiment")
        return _NEUTRAL.copy()

    try:
        from newsapi import NewsApiClient
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError as e:
        log.warning(f"Missing dependency: {e} — returning neutral news sentiment")
        return _NEUTRAL.copy()

    try:
        newsapi  = NewsApiClient(api_key=config.NEWSAPI_KEY)
        analyzer = SentimentIntensityAnalyzer()

        query = f'"{ticker}"'
        if company_name:
            query += f' OR "{company_name}"'

        from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

        response = newsapi.get_everything(
            q=query,
            language="en",
            sort_by="publishedAt",
            from_param=from_date,
            page_size=50,
        )

        articles_raw = response.get("articles", [])
        scored = []

        for article in articles_raw:
            title = article.get("title") or ""
            desc  = article.get("description") or ""
            text  = f"{title} {desc}".strip()
            if not text:
                continue

            vs = analyzer.polarity_scores(text)

            # Recency decay (exponential, half-life ≈ 7 hours)
            published = article.get("publishedAt", "")
            try:
                pub_dt    = datetime.strptime(published[:19], "%Y-%m-%dT%H:%M:%S")
                age_hours = (datetime.utcnow() - pub_dt).total_seconds() / 3600
            except Exception:
                log.debug(f"Unparseable publishedAt '{published}'; defaulting age_hours to 24")
                age_hours = 24
            recency_weight = math.exp(-0.1 * age_hours)

            # Source authority
            url = article.get("url", "")
            domain = _extract_domain(url)
            src_weight = SOURCE_WEIGHTS.get(domain, 0.8)

            weighted = vs["compound"] * recency_weight * src_weight
            scored.append({
                "headline":  title,
                "raw_score": round(vs["compound"], 4),
                "weighted":  round(weighted, 4),
                "source":    domain,
                "age_hours": round(age_hours, 1),
            })

        if not scored:
            return _NEUTRAL.copy()

        composite = sum(a["weighted"] for a in scored) / len(scored)
        signal = "BULLISH" if composite > 0.1 else "BEARISH" if composite < -0.1 else "NEUTRAL"

        return {
            "composite_score": round(composite, 4),
            "signal": signal,
            "article_count": len(scored),
            "articles": sorted(scored, key=lambda x: abs(x["weighted"]), reverse=True)[:5],
            "sentiment_score_0_100": int((composite + 1) / 2 * 100),
        }

    except Exception as e:
        log.error(f"{ticker}: news sentiment error: {e}")
        return _NEUTRAL.copy()


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""
