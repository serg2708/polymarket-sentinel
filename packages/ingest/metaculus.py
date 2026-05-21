"""Metaculus API client — expert/superforecaster median probabilities.

NOTE: Community prediction (CP) aggregations require "forecaster" API tier.
New accounts start as "restricted" and get null aggregations.
To request an upgrade: https://www.metaculus.com/api/
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..common.settings import get_settings

log = structlog.get_logger()

METACULUS_BASE = "https://www.metaculus.com/api2"
METACULUS_NEW_BASE = "https://www.metaculus.com/api"


def _auth_headers() -> dict:
    token = get_settings().metaculus_api_token
    if token:
        return {"Authorization": f"Token {token}"}
    return {}


def _extract_cp_from_new_api(post: dict) -> float | None:
    """Extract community median from new /api/posts/ response format."""
    q = post.get("question") or {}
    agg = q.get("aggregations") or {}
    rw = agg.get("recency_weighted") or {}
    latest = rw.get("latest") or {}
    if not latest:
        return None
    centers = latest.get("centers")
    if centers and len(centers) >= 2:
        # Binary question: centers[0]=P(No), centers[1]=P(Yes)
        return centers[1]
    means = latest.get("means")
    if means and len(means) >= 2:
        return means[1]
    return latest.get("median")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def get_question(qid: int) -> dict | None:
    headers = _auth_headers()
    if not headers:
        log.debug("metaculus_skipped_no_token", qid=qid)
        return None
    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        # Try new /api/posts/ first (has richer aggregation structure)
        try:
            r = await c.get(f"{METACULUS_NEW_BASE}/posts/{qid}/")
            r.raise_for_status()
            post = r.json()
            cp_new = _extract_cp_from_new_api(post)
            q = post.get("question") or {}
            # Fall back to old api2 format if new API has no CP data
            cp_median = cp_new
            if cp_median is None:
                # Try old /api2/questions/ endpoint
                try:
                    r2 = await c.get(f"{METACULUS_BASE}/questions/{q.get('id', qid)}/")
                    r2.raise_for_status()
                    d2 = r2.json()
                    cp2 = (d2.get("community_prediction") or {}).get("full") or {}
                    cp_median = cp2.get("q2")
                    if cp_median is None:
                        log.debug(
                            "metaculus_cp_unavailable",
                            post_id=qid,
                            note="restricted API tier — CP aggregations blocked",
                        )
                except Exception:
                    pass

            return {
                "question_id": qid,
                "title": post.get("title"),
                "community_median": cp_median,
                "resolved": q.get("resolution"),
                "close_time": _parse_dt(
                    q.get("actual_close_time") or q.get("scheduled_close_time")
                ),
                "raw": post,
            }
        except Exception as exc:
            log.warning("metaculus_error", qid=qid, error=str(exc))
            return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def search_questions(term: str, limit: int = 10) -> list[dict]:
    """Search via new /api/posts/ endpoint (old api2 search was broken)."""
    headers = _auth_headers()
    if not headers:
        return []
    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        r = await c.get(
            f"{METACULUS_NEW_BASE}/posts/",
            params={"status": "open", "search": term, "limit": limit},
            headers=headers,
        )
        r.raise_for_status()
        return r.json().get("results", [])


async def submit_forecast(post_id: int, question_id: int, probability: float) -> bool:
    """Submit a forecast to build account activity (helps tier upgrade).

    Returns True on success.
    """
    headers = _auth_headers()
    if not headers:
        return False
    probability = max(0.01, min(0.99, probability))
    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        try:
            r = await c.post(
                f"{METACULUS_BASE}/questions/{question_id}/predict/",
                json={"prediction": probability},
                headers=headers,
            )
            if r.status_code in (200, 201):
                log.debug("metaculus_forecast_submitted", post_id=post_id, p=probability)
                return True
            log.debug("metaculus_forecast_skipped", post_id=post_id, status=r.status_code, body=r.text[:100])
            return False
        except Exception as exc:
            log.warning("metaculus_forecast_error", post_id=post_id, error=str(exc))
            return False


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None
