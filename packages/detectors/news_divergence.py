"""News-divergence detector.

For each watched market, polls Redis for recent news articles, asks the
local LLM (via Ollama) to score each article's directional impact on the
market question, maintains an exponentially-weighted sentiment index, and
fires an alert when the index diverges significantly from Polymarket's price.

Signal logic:
  - sentiment_index in [-1, +1] (positive = YES-bullish news)
  - smoothed with EMA over recent articles
  - alert when abs(sentiment_index) > SENTIMENT_THRESHOLD
    AND Polymarket price hasn't moved proportionally (stale-price condition)
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone

import httpx
import structlog

from ..common.settings import get_settings

log = structlog.get_logger()

SENTIMENT_THRESHOLD = 0.4     # |EMA sentiment| must exceed this to alert
MIN_ARTICLES = 3              # need at least this many recent articles
EMA_ALPHA = 0.3               # EMA smoothing factor (higher = more weight on recent)
PRICE_STALE_SECONDS = 300     # Polymarket price considered stale if unchanged for this long


SENTIMENT_PROMPT = """\
You are analysing a news article to determine its directional impact on a \
prediction market question.

Market question: "{question}"
Current market probability (YES): {market_p:.2%}

Article headline: "{title}"
Article summary: "{summary}"

Rate the article's impact on P(YES):
- R (raises):   article is clearly positive for YES outcome
- L (lowers):   article is clearly negative for YES outcome
- U (unchanged): article is neutral or unrelated

Also rate your confidence (0.0–1.0).

Reply ONLY valid JSON: {{"direction": "R|L|U", "confidence": 0.0-1.0, "reason": "..."}}
"""


async def score_article(
    article: dict,
    question: str,
    market_p: float,
    model: str | None = None,
) -> dict | None:
    """Ask Claude (or Ollama fallback) to score one article's directional impact."""
    settings = get_settings()
    prompt = SENTIMENT_PROMPT.format(
        question=question,
        market_p=market_p,
        title=article.get("title", "")[:200],
        summary=article.get("summary", "")[:500],
    )

    async def _nvidia() -> dict | None:
        if not settings.nvidia_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{settings.nvidia_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.nvidia_api_key}"},
                    json={
                        "model": settings.nvidia_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 150,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                return _parse_json(content)
        except Exception as exc:
            log.warning("news_score_nvidia_error", error=str(exc))
            return None

    async def _ollama() -> dict | None:
        m = model or settings.ollama_model
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"{settings.ollama_base_url}/api/generate",
                    json={"model": m, "prompt": prompt, "stream": False, "format": "json"},
                )
                r.raise_for_status()
                return _parse_json(r.json().get("response", "{}"))
        except Exception as exc:
            log.warning("news_score_ollama_error", error=str(exc))
            return None

    async def _claude() -> dict | None:
        if not settings.anthropic_api_key:
            return None
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            return _parse_json(msg.content[0].text)
        except Exception as exc:
            log.warning("news_score_claude_error", error=str(exc))
            return None

    if settings.nvidia_api_key:
        return await _nvidia() or await _ollama()
    if settings.ollama_primary:
        return await _ollama()
    return await _claude() or await _ollama()


def _parse_json(raw: str) -> dict | None:
    import re
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def direction_to_score(direction: str, confidence: float) -> float:
    """Convert LLM direction+confidence to a scalar in [-1, +1]."""
    d = direction.upper()
    if d == "R":
        return confidence
    elif d == "L":
        return -confidence
    else:
        return 0.0


class SentimentIndex:
    """
    Exponentially-weighted moving average of article sentiment scores.
    Higher = more YES-bullish recent news.
    """

    def __init__(self, alpha: float = EMA_ALPHA) -> None:
        self.alpha = alpha
        self.ema: float | None = None
        self.n: int = 0

    def update(self, score: float) -> None:
        if self.ema is None:
            self.ema = score
        else:
            self.ema = self.alpha * score + (1.0 - self.alpha) * self.ema
        self.n += 1

    @property
    def value(self) -> float | None:
        return self.ema

    def is_diverging(self, market_p: float, threshold: float = SENTIMENT_THRESHOLD) -> bool:
        """True when news sentiment strongly disagrees with market price direction."""
        if self.ema is None or self.n < MIN_ARTICLES:
            return False
        # Sentiment > threshold AND market is NOT priced accordingly
        if self.ema > threshold and market_p < 0.7:
            return True
        if self.ema < -threshold and market_p > 0.3:
            return True
        return False


# Per-market sentiment indices
_sentiment_store: dict[str, SentimentIndex] = {}


def get_sentiment(group_key: str) -> SentimentIndex:
    if group_key not in _sentiment_store:
        _sentiment_store[group_key] = SentimentIndex()
    return _sentiment_store[group_key]


async def run_news_divergence_check(
    redis_client,
    group_key: str,
    question: str,
    market_p: float,
    topic: str | None = None,
) -> dict | None:
    """
    Main entry point: score recent articles for a market and check for divergence.
    Returns an alert dict if divergence detected, else None.
    """
    from ..ingest.news import pop_recent_articles

    from datetime import datetime, timezone, timedelta
    topic_key = topic or f"market:{group_key}"
    articles = await pop_recent_articles(redis_client, topic_key, n=10)

    # Drop articles older than 7 days and low-quality sources
    _bad_sources = {"nakedcapitalism", "nakedcapitalism.com", "unbiasthenews",
                    "unbiasthenews.com", "zerohedge", "zerohedge.com"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    def _pub(a):
        s = a.get("published_at") or a.get("published") or a.get("publishedAt") or ""
        try:
            dt = datetime.fromisoformat(s[:19].replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            return None
    articles = [
        a for a in articles
        if (a.get("source") or "").lower() not in _bad_sources
        and (_pub(a) is None or _pub(a) >= cutoff)
    ]

    if len(articles) < MIN_ARTICLES:
        return None

    index = get_sentiment(group_key)

    # Score new articles (batch, sequential to be Ollama-friendly)
    new_scores = []
    for art in articles[:5]:  # limit to 5 most recent per cycle
        verdict = await score_article(art, question, market_p)
        if verdict:
            score = direction_to_score(
                verdict.get("direction", "U"),
                float(verdict.get("confidence", 0.5)),
            )
            index.update(score)
            new_scores.append({
                "title": art.get("title", ""),
                "direction": verdict.get("direction"),
                "confidence": verdict.get("confidence"),
                "score": round(score, 3),
                "reason": verdict.get("reason", ""),
            })
        await asyncio.sleep(0.5)  # Ollama needs a breath

    if not index.is_diverging(market_p):
        return None

    return {
        "kind": "news_divergence",
        "group_key": group_key,
        "title": question,
        "sentiment_ema": round(index.value, 3),
        "market_p": round(market_p, 4),
        "n_articles_scored": index.n,
        "recent_articles": new_scores[:3],
        "edge_bps": int(abs(index.value) * 1000),  # proxy; not real arb bps
        "direction": "YES_underpriced" if index.value > 0 else "NO_underpriced",
    }
