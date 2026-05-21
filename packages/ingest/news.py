"""News ingest: RSS feeds + NewsAPI.

Collects articles relevant to watched markets and stores them for
the news-divergence detector and LLM sentiment scorer.

Storage: Redis list per group_key (capped at 50 articles), with a
"news:{group_key}:{url_hash}" dedup key.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
import structlog

from ..common.settings import get_settings

log = structlog.get_logger()

# ── RSS feed registry ─────────────────────────────────────────────────────
# Maps tag/topic → list of feed URLs to monitor.
# Extend this per your watched markets.

RSS_FEEDS: dict[str, list[str]] = {
    "crypto": [
        "https://feeds.feedburner.com/CoinDesk",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
    ],
    "us_politics": [
        "https://feeds.feedburner.com/politico/politics",
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://feeds.washingtonpost.com/rss/politics",
    ],
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "geopolitics": [
        "https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml",
        "https://foreignpolicy.com/feed/",
    ],
    "macro": [
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://feeds.bloomberg.com/markets/news.rss",
    ],
    "science_tech": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
    ],
    "sports_nfl": [
        "https://www.espn.com/espn/rss/nfl/news",
    ],
    "sports_nba": [
        "https://www.espn.com/espn/rss/nba/news",
    ],
    # Entertainment: Eurovision, Oscars, awards, cultural events
    "entertainment": [
        "https://variety.com/feed/",
        "https://deadline.com/feed/",
        "https://www.bbc.co.uk/sport/av/entertainment-and-arts/rss.xml",
    ],
}


async def fetch_feed(url: str, timeout: int = 20) -> list[dict]:
    """Fetch and parse a single RSS feed. Returns list of article dicts."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "PolySentinel/1.0 (news ingest; contact: bot@localhost)"},
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            content = r.text
    except Exception as exc:
        log.warning("rss_fetch_error", url=url, error=str(exc))
        return []

    loop = asyncio.get_event_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, content)

    articles = []
    for entry in feed.entries:
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import time
            published = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)

        articles.append({
            "url": entry.get("link", ""),
            "title": entry.get("title", ""),
            "summary": (entry.get("summary") or "")[:1000],
            "published": published.isoformat() if published else None,
            "source": feed.feed.get("title", url),
        })

    return articles


async def fetch_newsapi(query: str, days_back: int = 3) -> list[dict]:
    """Fetch articles from NewsAPI.org for a keyword query."""
    if not get_settings().newsapi_key:
        return []

    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "from": since,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "pageSize": 20,
                    "apiKey": get_settings().newsapi_key,
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.warning("newsapi_error", query=query, error=str(exc))
        return []

    articles = []
    for a in data.get("articles", []):
        articles.append({
            "url": a.get("url", ""),
            "title": a.get("title", ""),
            "summary": (a.get("description") or "")[:1000],
            "published": a.get("publishedAt"),
            "source": (a.get("source") or {}).get("name", ""),
        })
    return articles


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


async def ingest_topic_to_redis(
    redis_client,
    topic: str,
    articles: list[dict],
    max_per_topic: int = 50,
) -> int:
    """Dedup and push articles to Redis. Returns count of new articles added."""
    added = 0
    for art in articles:
        url = art.get("url", "")
        if not url:
            continue
        dedup_key = f"news_dedup:{topic}:{_url_hash(url)}"
        is_new = await redis_client.set(dedup_key, "1", nx=True, ex=86400 * 7)
        if is_new:
            list_key = f"news:{topic}"
            await redis_client.lpush(list_key, json.dumps(art))
            await redis_client.ltrim(list_key, 0, max_per_topic - 1)
            added += 1
    return added


async def pop_recent_articles(
    redis_client,
    topic: str,
    n: int = 10,
) -> list[dict]:
    """Read the N most recent articles for a topic (non-destructive LRANGE)."""
    raw_list = await redis_client.lrange(f"news:{topic}", 0, n - 1)
    result = []
    for raw in raw_list:
        try:
            result.append(json.loads(raw))
        except Exception:
            pass
    return result


async def refresh_all_feeds(redis_client) -> None:
    """Fetch all registered RSS feeds and push to Redis."""
    for topic, urls in RSS_FEEDS.items():
        for url in urls:
            articles = await fetch_feed(url)
            added = await ingest_topic_to_redis(redis_client, topic, articles)
            if added:
                log.debug("rss_articles_added", topic=topic, url=url, added=added)
        await asyncio.sleep(0.1)  # light throttle between topics


NEWSAPI_DAILY_LIMIT = 90  # leave 10-request buffer from free-tier 100/day

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "been",
    "did", "do", "does", "have", "has", "had", "by", "in", "of", "to",
    "for", "on", "at", "from", "into", "with", "would", "could", "should",
    "might", "may", "can", "before", "after", "during", "ever", "never",
    "between", "above", "below", "over", "under", "this", "that", "these",
    "those", "it", "its", "or", "and", "but", "yet", "so", "nor", "not",
    "no", "yes", "if", "when", "where", "who", "what", "which", "how",
    "why", "any", "all", "each", "than", "then", "there", "here", "out",
    "up", "down", "about", "2024", "2025", "2026", "2027",
})


def _build_search_query(question: str, max_terms: int = 6) -> str:
    """Extract meaningful search terms from a market question."""
    words = question.replace("?", "").replace(",", "").replace("'", "").split()
    terms = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
    return " ".join(terms[:max_terms]) if terms else " ".join(question.split()[:5])


async def _newsapi_quota_ok(redis_client) -> bool:
    """Increment the UTC-day request counter; return False if daily limit reached."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"newsapi_reqs:{today}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 90_000)  # 25 h — covers midnight rollover
    if count > NEWSAPI_DAILY_LIMIT:
        await redis_client.decr(key)
        return False
    return True


async def refresh_newsapi_for_markets(
    redis_client,
    market_questions: list[tuple[str, str]],
    days_back: int = 5,
) -> None:
    """
    For each (group_key, question) pair, search NewsAPI for relevant articles.
    Respects a hard daily quota of NEWSAPI_DAILY_LIMIT requests tracked in Redis.
    Uses stopword-filtered query extraction for better search quality.
    """
    if not get_settings().newsapi_key:
        return
    for group_key, question in market_questions:
        if not await _newsapi_quota_ok(redis_client):
            log.warning("newsapi_daily_quota_reached", limit=NEWSAPI_DAILY_LIMIT)
            break
        query = _build_search_query(question)
        articles = await fetch_newsapi(query, days_back=days_back)
        added = await ingest_topic_to_redis(redis_client, f"market:{group_key}", articles, max_per_topic=30)
        if added:
            log.debug("newsapi_articles_added", group_key=group_key, added=added, query=query)
        await asyncio.sleep(1.0)  # ~1 req/s is polite; real cap is the daily counter above
