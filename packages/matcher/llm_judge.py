"""LLM-based market equivalence judge.

Uses Ollama (local) or Anthropic Claude as fallback.
Returns a structured verdict: equivalent, confidence, polarity, rule_diff.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
import structlog

from ..common.settings import get_settings

log = structlog.get_logger()


JUDGE_PROMPT = """\
Two prediction markets. Decide if they resolve to the same real-world outcome.

A. {source_a}: "{question_a}"
   Resolution rules: {rules_a}

B. {source_b}: "{question_b}"
   Resolution rules: {rules_b}

Reply ONLY valid JSON (no markdown), no explanation outside the JSON:
{{
  "equivalent": <true|false>,
  "confidence": <0.0-1.0>,
  "polarity": "<same|opposite>",
  "rule_diff": "<brief description of rule differences, or null>"
}}
"""


async def judge_match_ollama(
    market_a: dict,
    market_b: dict,
    model: str | None = None,
) -> dict | None:
    settings = get_settings()
    m = model or settings.ollama_model
    prompt = JUDGE_PROMPT.format(
        source_a=market_a.get("source", "Market A"),
        question_a=market_a.get("question", ""),
        rules_a=(market_a.get("description") or "")[:1500],
        source_b=market_b.get("source", "Market B"),
        question_b=market_b.get("question", ""),
        rules_b=(market_b.get("description") or "")[:1500],
    )

    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": m, "prompt": prompt, "stream": False, "format": "json"},
            )
            r.raise_for_status()
            raw = r.json().get("response", "{}")
            return _parse_json(raw)
    except Exception as exc:
        log.warning("llm_judge_ollama_error", error=str(exc))
        return None


async def judge_match_claude(market_a: dict, market_b: dict) -> dict | None:
    """Fallback to Anthropic API when Ollama is unavailable or for high-stakes matches."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None

    try:
        import anthropic

        prompt = JUDGE_PROMPT.format(
            source_a=market_a.get("source", "Market A"),
            question_a=market_a.get("question", ""),
            rules_a=(market_a.get("description") or "")[:1500],
            source_b=market_b.get("source", "Market B"),
            question_b=market_b.get("question", ""),
            rules_b=(market_b.get("description") or "")[:1500],
        )

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        return _parse_json(raw)
    except Exception as exc:
        log.warning("llm_judge_claude_error", error=str(exc))
        return None


async def judge_match(market_a: dict, market_b: dict) -> dict | None:
    """Try Claude first (better quality), fall back to Ollama if no API key."""
    result = await judge_match_claude(market_a, market_b)
    if result is None:
        result = await judge_match_ollama(market_a, market_b)
    return result


def _parse_json(raw: str) -> dict | None:
    # Strip markdown code fences if present
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting JSON object
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    log.warning("llm_judge_parse_failure", raw=raw[:200])
    return None
