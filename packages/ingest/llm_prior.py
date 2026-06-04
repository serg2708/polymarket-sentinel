"""Claude-based market probability prior.

Asks Claude to estimate P(YES) for a Polymarket question given the
question text and recent news headlines. Results are cached in Redis
for LLM_PRIOR_TTL seconds to control API costs.

Why this works despite Claude's Aug-2025 knowledge cutoff:
  - Recent news headlines (from NewsAPI) are injected into the prompt
  - For structural questions (Fed policy, elections) the model is well-calibrated
  - For fast-moving events, news context compensates for the cutoff
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta

import structlog

from ..common.settings import get_settings

log = structlog.get_logger()

LLM_PRIOR_TTL = 6 * 3600   # 6 hours per market
REDIS_PREFIX = "llm_prior:"

PRIOR_PROMPT = """\
Today is {today}. You are a well-calibrated forecaster.

Predict the probability (0.00–1.00) that the following statement resolves YES.
Use available information and your best judgment. Be concise.

Question: {question}

Resolution criteria: {resolution}

{news_block}

Reply ONLY with valid JSON, no markdown:
{{"probability": <0.00-1.00>, "confidence": <0.0-1.0>, "brief_reason": "<1 sentence>"}}

Rules:
- probability must be a number between 0.01 and 0.99
- confidence reflects how certain you are (0.9 = very sure, 0.5 = uncertain)
- If the question is about events after your knowledge cutoff, rely on the news context provided
"""

NEWS_BLOCK_TEMPLATE = """\
Recent relevant headlines (newest first):
{headlines}
"""

_LOW_QUALITY_SOURCES = {
    "nakedcapitalism", "nakedcapitalism.com",
    "unbiasthenews", "unbiasthenews.com",
    "zerohedge", "zerohedge.com",
    "infowars", "breitbart",
}

def _article_date(a: dict) -> str:
    """Return ISO date string from whichever date field the article uses."""
    return (a.get("published_at") or a.get("published") or a.get("publishedAt") or "")[:10]

def _filter_news(news: list[dict], max_age_days: int = 7) -> list[dict]:
    """Remove stale and low-quality articles."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out = []
    for a in news:
        src = (a.get("source") or "").lower()
        if any(bad in src for bad in _LOW_QUALITY_SOURCES):
            continue
        date_str = _article_date(a)
        if date_str:
            try:
                pub = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub < cutoff:
                    continue
            except ValueError:
                pass
        out.append(a)
    return out


def _build_prompt(question: str, description: str, news: list[dict], today: str) -> str:
    news_block = ""
    fresh = _filter_news(news)
    if fresh:
        headlines = "\n".join(
            f"- [{_article_date(a)}] {a.get('title', '')}"
            for a in fresh[:6]
        )
        news_block = NEWS_BLOCK_TEMPLATE.format(headlines=headlines)

    resolution = (description or "")[:800] or "Same as question title."
    return PRIOR_PROMPT.format(
        today=today,
        question=question[:300],
        resolution=resolution,
        news_block=news_block,
    )


def _parse_response(raw: str) -> dict | None:
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


async def _call_nvidia_nim(prompt: str, settings) -> dict | None:
    """Call NVIDIA NIM (OpenAI-compatible) and return parsed JSON dict."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(
                f"{settings.nvidia_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.nvidia_api_key}"},
                json={
                    "model": settings.nvidia_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 300,
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return _parse_response(content)
    except Exception as exc:
        log.warning("llm_prior_nvidia_error", error=str(exc))
        return None


async def _call_ollama(prompt: str, settings) -> dict | None:
    """Call local Ollama and return parsed JSON dict, or None on failure."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": settings.ollama_model, "prompt": prompt,
                      "stream": False, "format": "json"},
            )
            r.raise_for_status()
            return _parse_response(r.json().get("response", "{}"))
    except Exception as exc:
        log.warning("llm_prior_ollama_error", error=str(exc))
        return None


async def _call_claude(prompt: str, market_id: str, settings,
                       model: str = "claude-haiku-4-5-20251001") -> dict | None:
    """Call Claude API and return parsed JSON dict, or None on failure."""
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=model, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_response(msg.content[0].text)
    except Exception as exc:
        log.warning("llm_prior_claude_error", market_id=market_id, error=str(exc))
        return None


async def estimate_probability(
    market_id: str,
    question: str,
    description: str,
    redis_client,
    today: str,
    recent_news: list[dict] | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[float, float] | None:
    """Return (probability, confidence) for a market, or None on failure.

    Cached in Redis for LLM_PRIOR_TTL seconds. When OLLAMA_PRIMARY=true,
    Ollama is tried first and Claude is used only as fallback.
    """
    settings = get_settings()

    cache_key = f"{REDIS_PREFIX}{market_id}"
    cached = await redis_client.get(cache_key)
    if cached:
        try:
            obj = json.loads(cached)
            return float(obj["p"]), float(obj.get("confidence", 0.5))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass

    prompt = _build_prompt(question, description, recent_news or [], today)

    if settings.nvidia_api_key:
        parsed = await _call_nvidia_nim(prompt, settings) or await _call_ollama(prompt, settings)
    elif settings.ollama_primary:
        parsed = await _call_ollama(prompt, settings)
    else:
        parsed = await _call_claude(prompt, market_id, settings, model) or await _call_ollama(prompt, settings)

    if not parsed:
        return None

    p = float(parsed.get("probability", -1))
    conf = float(parsed.get("confidence", 0.5))
    reason = parsed.get("brief_reason", "")

    if not (0.01 <= p <= 0.99):
        return None

    log.info("llm_prior_estimated", market_id=market_id,
             p=round(p, 3), confidence=round(conf, 2), reason=reason[:80])

    await redis_client.setex(cache_key, LLM_PRIOR_TTL, json.dumps({"p": p, "confidence": conf}))
    return p, conf


# ── Tail-risk underpricing analysis ──────────────────────────────────────────

TAIL_RISK_PROMPT = """\
Today is {today}.

You are analyzing a low-probability prediction market for potential underpricing.
The market currently prices this event at only {market_p_pct}%.

Question: {question}

Resolution criteria: {resolution}

{news_block}

Your task: determine whether this event is MORE likely than {market_p_pct}% given
current available information.

High-value tail risk patterns to look for:
- Geopolitical: assassination/coup/regime change attempts, military escalation, sanctions
- Multi-outcome competitions: Eurovision, Oscars, Nobel — strong recent performance or buzz
- Regulatory surprise: unexpected approval/rejection, policy reversal
- Financial: sovereign default, surprise rate move, company collapse
- Black swan: low-base-rate events with fresh catalysts

Reply ONLY with valid JSON, no markdown:
{{"probability": <0.02-0.80>, "confidence": <0.0-1.0>, "underpriced": <true|false>, "key_signal": "<one concrete sentence — the single fact that most changes the probability>"}}

Rules:
- underpriced=true ONLY when specific evidence genuinely raises the probability above the market
- confidence = quality of evidence: 0.9 = strong fresh specific news, 0.65 = moderate signal, 0.4 = speculative
- If NO relevant news provided but you have strong pre-cutoff knowledge (structural facts, historical base rates, known tournament dynamics): confidence may reach 0.65 and underpriced=true if justified
- If truly no information at all: confidence ≤ 0.50 and underpriced=false
- key_signal must be a specific fact or structural reason, not vague reasoning
- probability capped at 0.80 (tail risks remain tail risks even when elevated)
"""


TAIL_RISK_CACHE_TTL = 4 * 3600   # 4 hours — re-evaluate when news changes


async def estimate_tail_risk(
    market_id: str,
    question: str,
    description: str,
    market_p: float,
    redis_client,
    today: str,
    recent_news: list[dict] | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[float, float, bool, str] | None:
    """Analyse whether a low-probability market is underpriced given recent news.

    Returns (probability, confidence, underpriced, key_signal) or None on failure.
    Cached 4h keyed on market_id + top-3 news titles to avoid re-calling when
    the same headlines are already evaluated. When OLLAMA_PRIMARY=true, Ollama
    is tried first and Claude is used only as fallback.
    """
    settings = get_settings()

    # Cache key includes top-3 headlines so a news change busts the cache
    news_fp = "|".join(a.get("title", "")[:60] for a in (recent_news or [])[:3])
    cache_key = f"tail_risk_cache:{market_id}:{hash(news_fp) & 0xFFFFFFFF}"
    cached = await redis_client.get(cache_key)
    if cached:
        try:
            obj = json.loads(cached)
            return (float(obj["p"]), float(obj["conf"]),
                    bool(obj["underpriced"]), str(obj["key_signal"]))
        except Exception:
            pass

    news_block = ""
    fresh_news = _filter_news(recent_news or [])
    if fresh_news:
        headlines = "\n".join(
            f"- [{_article_date(a)}] {a.get('title', '')}"
            for a in fresh_news[:8]
        )
        news_block = f"Recent relevant headlines (newest first):\n{headlines}"

    resolution = (description or "")[:800] or "Same as question title."
    prompt = TAIL_RISK_PROMPT.format(
        today=today,
        market_p_pct=round(market_p * 100, 1),
        question=question[:300],
        resolution=resolution,
        news_block=news_block,
    )

    if settings.nvidia_api_key:
        parsed = await _call_nvidia_nim(prompt, settings) or await _call_ollama(prompt, settings)
    elif settings.ollama_primary:
        parsed = await _call_ollama(prompt, settings)
    else:
        parsed = await _call_claude(prompt, market_id, settings, model) or await _call_ollama(prompt, settings)

    if not parsed:
        return None

    p = float(parsed.get("probability", -1))
    conf = float(parsed.get("confidence", 0.0))
    underpriced = bool(parsed.get("underpriced", False))
    key_signal = str(parsed.get("key_signal", ""))

    if not (0.02 <= p <= 0.80):
        return None

    log.info("tail_risk_estimated", market_id=market_id,
             market_p=round(market_p, 3), claude_p=round(p, 3),
             confidence=round(conf, 2), underpriced=underpriced,
             signal=key_signal[:80])

    await redis_client.setex(cache_key, TAIL_RISK_CACHE_TTL,
                             json.dumps({"p": p, "conf": conf,
                                         "underpriced": underpriced,
                                         "key_signal": key_signal}))
    return p, conf, underpriced, key_signal
