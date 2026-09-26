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

import httpx
import structlog

from ..common.settings import get_settings

log = structlog.get_logger()

LLM_PRIOR_TTL = 6 * 3600   # 6 hours per market
REDIS_PREFIX = "llm_prior:"

# Shared guard against the most common multi-bracket failure: folding
# "the event probably won't happen" into the lowest numeric bracket. On
# Polymarket these are SEPARATE outcomes — "No IPO / did not happen" is its
# own ticket and never pays the "< $X" bracket.
BRACKET_RULES = """\
CRITICAL — bracketed / multi-outcome markets:
- If this question is ONE bracket of a set (e.g. "market cap between $X and $Y",
  "less than $X", a specific numeric range, "between A and B"), it resolves YES
  ONLY IF the underlying event actually happens AND the measured value lands in
  THIS exact bracket.
- A separate "No event / No IPO / did not occur by <date>" outcome is its OWN
  ticket. NEVER fold "the event probably will not happen" into a low numeric
  bracket. "Event won't occur" pays the No-event outcome, NOT the lowest number.
- For a "less than $X" bracket: YES needs the event to occur AND land below $X.
  If $X is far below a recent known reference (last funding round / valuation /
  price), a YES here is a down-round / collapse scenario and is VERY unlikely
  even if the event itself is uncertain. Treat such brackets as low probability.
- "Between X and Y" is a BOUNDED BAND, not a threshold. "It will exceed X" is a
  DIFFERENT claim and does NOT support this bracket — exceeding X by a lot lands
  in a HIGHER bracket and pays nothing here. Evidence that the value will be
  strong/high is an argument AGAINST a band that sits below the expected level.
  A narrow band deserves a low probability unless the expected value lands
  squarely inside it.

CRITICAL — "hit (HIGH) $X" / "hit (LOW) $X" touch markets:
- "(HIGH) $X" resolves YES only if the price RISES to $X or above. "(LOW) $X"
  resolves YES only if the price FALLS to $X or below. They are opposite legs.
- Check the direction of your own evidence before answering. News that prices
  are RISING supports the (HIGH) legs and argues AGAINST every (LOW) leg, and
  vice versa. Never cite an upward catalyst as a reason a (LOW) market is
  underpriced.

CRITICAL — "will X launch a token" markets:
- These resolve YES only for the project's OWN network/governance token (a TGE
  with tokenomics, snapshot/airdrop, or listing). Read the resolution rules:
  stablecoins, memecoins, LSTs, synthetic tokens, and "creator coins" usually
  do NOT count.
- A token STANDARD, issuance framework, precompile, "native token standard",
  developer/airdrop DEMO, or a tool that lets OTHERS create tokens is NOT the
  project launching its own token. Do not treat such news as a catalyst for YES.
  (e.g. an L2 shipping a token-issuance standard for stablecoin/RWA issuers is
  infrastructure, not its own network token.)
- Require an explicit, dated company statement about ITS OWN token (name,
  tokenomics, snapshot, or launch window) before raising probability. Vague
  "exploring a token, no timeline" guidance keeps probability low."""

PRIOR_PROMPT = """\
Today is {today}. You are a well-calibrated forecaster.

Predict the probability (0.00–1.00) that the following statement resolves YES.
Use available information and your best judgment. Be concise.

Question: {question}

Resolution criteria: {resolution}

{news_block}

{bracket_rules}

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

# Numeric money/threshold detection for the bracket sanity gate
# Polymarket writes amounts both as "$1.2b" and bare "940m" (no dollar sign), and
# commodity ladders use a plain "$80" with no magnitude suffix. Match all three.
_MONEY_RE = re.compile(
    r"\$\s?\d[\d.,]*\s?(?:billion|trillion|million|[bmt])?\b"
    r"|\b\d[\d.,]*\s?(?:billion|trillion|million|[bmt])\b", re.IGNORECASE)
_BRACKET_RE = re.compile(
    r"\b(between|less than|greater than|at least|no more than|or greater|or more|"
    r"or less|below|above|under|over)\b", re.IGNORECASE)
_NO_EVENT_RE = re.compile(
    r"\bif no\b|\bno ipo\b|\bdoes not\b|\bno such\b|did not (?:occur|happen)|"
    r'resolve(?:s|d)? to ["\']?no\b', re.IGNORECASE)
_LOW_BRACKET_RE = re.compile(
    r"\b(less than|below|under|no more than|or less)\b\s*\$?\s?\d", re.IGNORECASE)
# Two-sided band: one slot of a ladder ("between 940m and 950m").
_BAND_RE = re.compile(
    r"\bbetween\b\s*\$?\s?\d[\d.,]*\s?(?:billion|trillion|million|[bmt])?\s*"
    r"(?:and|to|[-–—])\s*\$?\s?\d", re.IGNORECASE)
# Downside touch leg of a commodity ladder: "hit (LOW) $80".
_LOW_TOUCH_RE = re.compile(r"\(?\s*\blow\b\s*\)?\s*\$?\s?\d", re.IGNORECASE)


def is_multibracket_numeric(question: str, description: str) -> bool:
    """True when a market is one numeric bracket of a multi-outcome set that
    ALSO has a separate 'No event' outcome — the structure where an LLM tends
    to fold 'event won't happen' into the lowest number bracket."""
    text = f"{question}\n{description}"
    has_money = bool(_MONEY_RE.search(text))
    has_bracket = bool(_BRACKET_RE.search(text))
    has_no_event = bool(_NO_EVENT_RE.search(description or ""))
    return has_money and has_bracket and has_no_event


def is_low_numeric_bracket(question: str) -> bool:
    """True for the lowest 'less than $X' bracket — the one most often mis-mapped."""
    return bool(_LOW_BRACKET_RE.search(question or ""))


def is_bounded_band(question: str) -> bool:
    """True for a two-sided numeric band ("between 940m and 950m") — one slot of
    a ladder. An LLM arguing the value will be *high* is supporting some OTHER
    slot, not this one; "above X" and "in [X, Y]" are different claims."""
    return bool(_BAND_RE.search(question or ""))


def is_low_touch_threshold(question: str) -> bool:
    """True for the downside leg of a touch ladder ("hit (LOW) $80"), which
    resolves YES only if the price FALLS to the level. A bullish argument here
    is pointing at the (HIGH) legs instead."""
    return bool(_LOW_TOUCH_RE.search(question or ""))


def bracket_phantom_edge_reason(question: str, description: str) -> str | None:
    """Reason why an 'underpriced → BUY YES' on this market is structurally
    suspect, or None when nothing looks wrong.

    tail_risk and llm_prior only ever fire in the BUY YES direction, so these
    ladder shapes are where a phantom edge shows up: the model reasons about a
    threshold ("will exceed X", "prices are rising") and attaches the conclusion
    to a slot that needs something narrower or the opposite direction.
    """
    if is_multibracket_numeric(question, description) and is_low_numeric_bracket(question):
        return ("low numeric bracket of multi-outcome market — likely "
                "'no event' conflated into '< $X'")
    if is_bounded_band(question):
        return ("two-sided numeric band — 'the value will be high' argues for a "
                "different slot of the ladder, not this bounded one")
    if is_low_touch_threshold(question):
        return ("'(LOW) $X' touch market resolves YES only if the price FALLS "
                "to $X — a bullish signal here supports the (HIGH) legs")
    return None


def _article_date(a: dict) -> str:
    """Return ISO date string from whichever date field the article uses."""
    return (a.get("published_at") or a.get("published") or a.get("publishedAt") or "")[:10]

def _dedup_key(a: dict) -> str:
    """Identity for de-duplication: prefer URL, else a normalised title.

    The same story often arrives via multiple feeds (or the same feed twice).
    Counting copies as distinct catalysts let a single news item clear the
    '>=2 fresh articles' gate, so collapse them first."""
    url = (a.get("url") or "").strip().lower()
    if url:
        return url.split("?")[0].rstrip("/")  # drop query/trailing slash
    title = (a.get("title") or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def _filter_news(news: list[dict], max_age_days: int = 7) -> list[dict]:
    """Remove stale, low-quality, and duplicate articles."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out, seen = [], set()
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
        key = _dedup_key(a)
        if key in seen:
            continue
        seen.add(key)
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

    resolution = (description or "")[:1000] or "Same as question title."
    return PRIOR_PROMPT.format(
        today=today,
        question=question[:300],
        resolution=resolution,
        news_block=news_block,
        bracket_rules=BRACKET_RULES,
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
    """Call NVIDIA NIM (OpenAI-compatible) and return parsed JSON dict.

    Uses the 70B reasoning model. On free tier it can take ~100s under load,
    so the timeout is generous (180s); Ollama is the fallback if it still fails.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=180) as c:
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
        log.warning("llm_prior_nvidia_error", error=f"{type(exc).__name__}: {exc}")
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


def _snippet(a: dict, n: int = 220) -> str:
    text = (a.get("description") or a.get("summary") or "").replace("\n", " ").strip()
    return text[:n]


def llm_backend_label() -> str:
    """Which model actually answers tail_risk / llm_prior, for alert text."""
    s = get_settings()
    if s.nvidia_api_key:
        return s.nvidia_model
    if s.ollama_primary:
        return s.ollama_model
    return "claude-haiku-4.5"


GAMMA = "https://gamma-api.polymarket.com"


async def sibling_markets(event_slug: str, market_id: str,
                          end_date: str | None = None) -> tuple[str, list[dict]]:
    """Other markets of the same event (e.g. "by Sep 22 / Sep 30 / Oct 31").

    Returns (prompt block, earlier-deadline siblings that resolved NO). Never raises.
    """
    if not event_slug:
        return "", []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{GAMMA}/events", params={"slug": event_slug})
            r.raise_for_status()
            events = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("sibling_markets_error", event_slug=event_slug, error=str(exc))
        return "", []
    lines, resolved_no, own_end = [], [], (end_date or "")[:10]
    markets = (events[0].get("markets") if events else None) or []
    for m in markets:
        if str(m.get("id")) == str(market_id):
            own_end = own_end or (m.get("endDate") or "")[:10]
    for m in markets:
        if str(m.get("id")) == str(market_id):
            continue
        try:
            raw = m.get("outcomePrices") or "[]"
            prices = [float(x) for x in (json.loads(raw) if isinstance(raw, str) else raw)]
        except (ValueError, TypeError):
            continue
        if len(prices) != 2:
            continue
        q, end = (m.get("question") or "")[:120], (m.get("endDate") or "")[:10]
        if m.get("closed") and max(prices) >= 0.99:
            outcome = "YES" if prices[0] > prices[1] else "NO"
            lines.append(f'- "{q}" — RESOLVED {outcome} (deadline {end})')
            if outcome == "NO" and own_end and end < own_end:
                resolved_no.append({"question": q, "end": end})
        elif not m.get("closed"):
            lines.append(f'- "{q}" — trading at {prices[0] * 100:.0f}% (deadline {end})')
    if not lines:
        return "", []
    return "Other markets of the same event:\n" + "\n".join(lines[:12]), resolved_no


# ── Tail-risk underpricing analysis ──────────────────────────────────────────

TAIL_RISK_PROMPT = """\
Today is {today}.

You are analyzing a low-probability prediction market for potential underpricing.
The market currently prices this event at only {market_p_pct}%.

Question: {question}

Resolution criteria: {resolution}

{siblings_block}

{news_block}

{bracket_rules}

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
- RESOLUTION MECHANISM FIRST. Many markets resolve on an announcement by a named
  source (a government, agency, company), not on the event itself. News that the
  event happened "according to sources" / "reportedly" / per a third party does NOT
  satisfy a criterion that requires an official announcement from that source.
  If the criteria exclude third-party claims, such reports are NOT evidence of YES.
- SIBLING MARKETS are hard evidence. If an earlier-deadline market of the same
  event resolved NO even though the news you see predates that deadline, that
  news did not meet the criteria — do not count it again here.
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
    siblings_block: str = "",
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
        # Snippet, not just the title: headlines drop qualifiers like "sources say"
        # that decide whether a report meets the resolution criteria.
        headlines = "\n".join(
            f"- [{_article_date(a)}] {a.get('title', '')}"
            + (f"\n  {_snippet(a)}" if _snippet(a) else "")
            for a in fresh_news[:8]
        )
        news_block = f"Recent relevant headlines (newest first):\n{headlines}"

    # Full rules: exclusions and the resolution source sit at the end of the text.
    resolution = (description or "")[:6000] or "Same as question title."
    prompt = TAIL_RISK_PROMPT.format(
        today=today,
        market_p_pct=round(market_p * 100, 1),
        question=question[:300],
        resolution=resolution,
        siblings_block=siblings_block,
        news_block=news_block,
        bracket_rules=BRACKET_RULES,
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

    # Sanity gate: tail risk always fires "underpriced → BUY YES". On the lowest
    # numeric bracket of a multi-outcome market ("< $X" with a separate "No event"
    # outcome), an underpriced YES is almost always the LLM folding "event won't
    # happen" into the low bracket — a phantom edge. Suppress and flag for review.
    bracket_reason = bracket_phantom_edge_reason(question, description) if underpriced else None
    if bracket_reason:
        log.warning("tail_risk_bracket_suppressed", market_id=market_id,
                    question=question[:80], claude_p=round(p, 3),
                    reason=f"{bracket_reason}; manual review")
        underpriced = False

    log.info("tail_risk_estimated", market_id=market_id,
             market_p=round(market_p, 3), claude_p=round(p, 3),
             confidence=round(conf, 2), underpriced=underpriced,
             signal=key_signal[:80])

    await redis_client.setex(cache_key, TAIL_RISK_CACHE_TTL,
                             json.dumps({"p": p, "conf": conf,
                                         "underpriced": underpriced,
                                         "key_signal": key_signal}))
    return p, conf, underpriced, key_signal
