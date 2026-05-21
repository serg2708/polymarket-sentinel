"""Manifold Markets API client (play-money, free)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger()

MANIFOLD_BASE = "https://api.manifold.markets/v0"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def search_markets(term: str, limit: int = 20) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{MANIFOLD_BASE}/search-markets",
            params={"term": term, "limit": limit, "filter": "open"},
        )
        r.raise_for_status()
        return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def get_market(market_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(f"{MANIFOLD_BASE}/market/{market_id}")
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("manifold_get_market_error", market_id=market_id, error=str(exc))
            return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def get_market_by_slug(slug: str) -> dict | None:
    """Fetch a Manifold market by slug.

    For MULTIPLE_CHOICE markets, use 'slug#AnswerText' syntax to extract a
    specific answer probability. Example: 'which-company-has-best-ai#Anthropic'
    Returns a dict with 'probability' set to the matching answer's probability.
    """
    answer_filter: str | None = None
    if "#" in slug:
        slug, answer_filter = slug.split("#", 1)

    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(f"{MANIFOLD_BASE}/slug/{slug}")
            r.raise_for_status()
            m = r.json()

            if answer_filter and m.get("outcomeType") == "MULTIPLE_CHOICE":
                answers = m.get("answers") or []
                match = next(
                    (a for a in answers if a.get("text", "").lower() == answer_filter.lower()),
                    None,
                )
                if match:
                    m = {**m, "probability": match.get("probability")}
                else:
                    log.warning("manifold_answer_not_found", slug=slug, answer=answer_filter,
                                available=[a.get("text") for a in answers])
                    return None

            return m
        except Exception as exc:
            log.warning("manifold_slug_error", slug=slug, error=str(exc))
            return None


def parse_manifold_market(m: dict) -> dict:
    prob = m.get("probability")
    return {
        "market_id": f"manifold:{m['id']}",
        "source": "manifold",
        "question": m.get("question"),
        "description": m.get("textDescription", ""),
        "slug": m.get("slug"),
        "end_date": _parse_dt(m.get("closeTime")),
        "active": not m.get("isResolved", True),
        "raw": m,
        "implied_prob": float(prob) if prob is not None else None,
    }


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.utcfromtimestamp(v / 1000)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None
