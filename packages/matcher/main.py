"""Matcher service entry point.

1. Loads manual_map.yaml → writes to market_matches with confidence=1.0
2. Runs embedding-based auto-matching for unmatched markets
3. LLM judge on high-scoring embedding candidates
4. Schedules periodic re-matching as new markets appear
"""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import asyncpg
import httpx
import structlog
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..common.db import get_pool, close_pool, upsert_match
from ..common.metrics import start_metrics_server, LLM_JUDGE_TOTAL, APPROVED_PAIRS
from ..common.settings import get_settings
from .embeddings import index_markets, find_matches
from .llm_judge import judge_match

log = structlog.get_logger()
settings = get_settings()

MANUAL_MAP_PATH = Path(__file__).parent / "manual_map.yaml"
AUTO_MATCH_LLM_THRESHOLD = 0.85   # only auto-approve if LLM confidence above this
EMBED_CANDIDATE_THRESHOLD = 0.78  # cosine similarity for candidate shortlist

MANIFOLD_MARKETS_URL = "https://api.manifold.markets/v0/search-markets"
MANIFOLD_FETCH_LIMIT = 500


async def load_manual_map(pool: asyncpg.Pool) -> None:
    """Parse manual_map.yaml, upsert current entries, and delete stale ones."""
    if not MANUAL_MAP_PATH.exists():
        return

    data = yaml.safe_load(MANUAL_MAP_PATH.read_text())
    markets = data.get("markets", [])

    # Build the set of (group_key, source, source_id) that SHOULD exist
    valid: set[tuple[str, str, str]] = set()

    for m in markets:
        group_key = m["key"]

        if m.get("polymarket"):
            valid.add((group_key, "polymarket", str(m["polymarket"])))
            await upsert_match(pool, {
                "group_key": group_key,
                "source": "polymarket",
                "source_id": m["polymarket"],
                "side": m.get("poly_side", "YES"),
                "match_score": 1.0,
                "llm_confidence": 1.0,
                "rule_notes": m.get("notes"),
                "approved_by": "manual",
            })

        if m.get("kalshi"):
            valid.add((group_key, "kalshi", str(m["kalshi"])))
            await upsert_match(pool, {
                "group_key": group_key,
                "source": "kalshi",
                "source_id": m["kalshi"],
                "side": m.get("kalshi_side", "YES"),
                "match_score": 1.0,
                "llm_confidence": 1.0,
                "rule_notes": m.get("notes"),
                "approved_by": "manual",
            })

        for src in ("manifold", "metaculus", "predictit"):
            if m.get(src):
                sid = str(m[src])
                valid.add((group_key, src, sid))
                await upsert_match(pool, {
                    "group_key": group_key,
                    "source": src,
                    "source_id": sid,
                    "side": "YES",
                    "match_score": 1.0,
                    "llm_confidence": 1.0,
                    "rule_notes": m.get("notes"),
                    "approved_by": "manual",
                })

    # Delete manual entries that are no longer in the YAML
    stale = await pool.fetch(
        "SELECT group_key, source, source_id FROM market_matches WHERE approved_by = 'manual'"
    )
    deleted = 0
    for row in stale:
        if (row["group_key"], row["source"], row["source_id"]) not in valid:
            await pool.execute(
                "DELETE FROM market_matches WHERE group_key=$1 AND source=$2 AND source_id=$3",
                row["group_key"], row["source"], row["source_id"],
            )
            deleted += 1

    log.info("manual_map_loaded", path=str(MANUAL_MAP_PATH), upserted=len(valid), deleted_stale=deleted)


async def auto_match_polymarket_vs_kalshi(pool: asyncpg.Pool) -> None:
    """Fetch unmatched Polymarket+Kalshi markets, embed, find candidates, LLM judge."""
    # Load markets from DB
    poly_markets = await pool.fetch(
        """
        SELECT market_id, question, description FROM markets
        WHERE source='polymarket' AND active=true
          AND market_id NOT IN (
            SELECT source_id FROM market_matches WHERE source='polymarket'
          )
        ORDER BY updated_at DESC LIMIT 500
        """
    )
    kalshi_markets = await pool.fetch(
        """
        SELECT market_id, question, description FROM markets
        WHERE source='kalshi' AND active=true
        ORDER BY updated_at DESC LIMIT 500
        """
    )

    if not poly_markets or not kalshi_markets:
        return

    log.info("auto_match_starting",
             n_poly=len(poly_markets), n_kalshi=len(kalshi_markets))

    # Index Kalshi markets
    kalshi_dicts = [dict(r) for r in kalshi_markets]
    await index_markets(kalshi_dicts, source="kalshi")

    # Find embedding candidates for each Polymarket market
    poly_dicts = [dict(r) for r in poly_markets]
    candidates = await find_matches(
        poly_dicts,
        target_source="kalshi",
        threshold=EMBED_CANDIDATE_THRESHOLD,
    )

    log.info("embedding_candidates", count=len(candidates))

    # Build lookup maps
    kalshi_by_id = {m["market_id"]: dict(m) for m in kalshi_markets}
    poly_by_id = {m["market_id"]: dict(m) for m in poly_markets}

    approved = 0
    for cand in candidates:
        poly_id = cand["query_id"]
        kalshi_id = cand["match_id"]
        embed_score = cand["score"]

        poly_m = poly_by_id.get(poly_id)
        kalshi_m = kalshi_by_id.get(kalshi_id)
        if not poly_m or not kalshi_m:
            continue

        # LLM judge
        verdict = await judge_match(
            {**poly_m, "source": "polymarket"},
            {**kalshi_m, "source": "kalshi"},
        )

        if verdict is None:
            LLM_JUDGE_TOTAL.labels(result="failed").inc()
            log.warning("llm_judge_failed", poly_id=poly_id, kalshi_id=kalshi_id)
            # Queue for manual review
            group_key = f"auto:{poly_id[:20]}"
            await upsert_match(pool, {
                "group_key": group_key, "source": "polymarket",
                "source_id": poly_id, "match_score": embed_score,
                "llm_confidence": 0.0, "approved_by": "pending",
            })
            continue

        confidence = float(verdict.get("confidence", 0))
        polarity = verdict.get("polarity", "same")
        rule_diff = verdict.get("rule_diff")
        equivalent = verdict.get("equivalent", False)

        if not equivalent:
            LLM_JUDGE_TOTAL.labels(result="rejected").inc()
            continue

        # Decide kalshi_side based on polarity
        kalshi_side = "YES" if polarity == "same" else "NO"

        group_key = f"auto:{poly_id[:20]}"
        approved_by = "auto" if confidence >= AUTO_MATCH_LLM_THRESHOLD else "pending"

        await upsert_match(pool, {
            "group_key": group_key, "source": "polymarket",
            "source_id": poly_id, "side": "YES",
            "match_score": embed_score, "llm_confidence": confidence,
            "rule_notes": rule_diff, "approved_by": approved_by,
        })
        await upsert_match(pool, {
            "group_key": group_key, "source": "kalshi",
            "source_id": kalshi_id, "side": kalshi_side,
            "match_score": embed_score, "llm_confidence": confidence,
            "rule_notes": rule_diff, "approved_by": approved_by,
        })

        LLM_JUDGE_TOTAL.labels(result="approved" if approved_by == "auto" else "pending").inc()
        if approved_by == "auto":
            approved += 1

    APPROVED_PAIRS.set(approved)
    log.info("auto_match_done", approved=approved, total_candidates=len(candidates))


async def fetch_manifold_markets(limit: int = MANIFOLD_FETCH_LIMIT) -> list[dict]:
    """Fetch top BINARY Manifold markets sorted by liquidity."""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                MANIFOLD_MARKETS_URL,
                params={"term": "", "sort": "liquidity", "limit": limit,
                        "contractType": "BINARY", "filter": "open"},
            )
            r.raise_for_status()
            raw = r.json()
    except Exception as exc:
        log.warning("manifold_fetch_error", error=str(exc))
        return []

    result = []
    for m in raw:
        if m.get("isResolved"):
            continue
        slug = m.get("slug", "")
        if not slug:
            continue
        result.append({
            "market_id": slug,
            "question": m.get("question", ""),
            "description": (m.get("textDescription") or "")[:2000],
        })

    log.info("manifold_markets_fetched", total=len(raw), binary_active=len(result))
    return result


async def auto_match_polymarket_vs_manifold(pool: asyncpg.Pool) -> None:
    """Find Polymarket↔Manifold pairs via embeddings + LLM judge."""
    poly_markets = await pool.fetch(
        """
        SELECT market_id, question, description FROM markets
        WHERE source='polymarket' AND active=true
          AND market_id NOT IN (
            SELECT source_id FROM market_matches WHERE source='polymarket'
          )
        ORDER BY updated_at DESC LIMIT 500
        """
    )
    if not poly_markets:
        log.info("manifold_auto_match_skip", reason="no_unmatched_poly_markets")
        return

    manifold_markets = await fetch_manifold_markets()
    if not manifold_markets:
        return

    log.info("manifold_auto_match_starting",
             n_poly=len(poly_markets), n_manifold=len(manifold_markets))

    await index_markets(manifold_markets, source="manifold")

    poly_dicts = [dict(r) for r in poly_markets]
    candidates = await find_matches(
        poly_dicts,
        target_source="manifold",
        threshold=EMBED_CANDIDATE_THRESHOLD,
    )

    log.info("manifold_embedding_candidates", count=len(candidates))

    manifold_by_id = {m["market_id"]: m for m in manifold_markets}
    poly_by_id = {m["market_id"]: dict(m) for m in poly_markets}

    approved = 0
    for cand in candidates:
        poly_id = cand["query_id"]
        manifold_slug = cand["match_id"]
        embed_score = cand["score"]

        poly_m = poly_by_id.get(poly_id)
        manifold_m = manifold_by_id.get(manifold_slug)
        if not poly_m or not manifold_m:
            continue

        verdict = await judge_match(
            {**poly_m, "source": "polymarket"},
            {**manifold_m, "source": "manifold"},
        )

        if verdict is None:
            LLM_JUDGE_TOTAL.labels(result="failed").inc()
            log.warning("manifold_judge_failed", poly_id=poly_id, slug=manifold_slug)
            continue

        confidence = float(verdict.get("confidence", 0))
        equivalent = verdict.get("equivalent", False)
        rule_diff = verdict.get("rule_diff")

        if not equivalent:
            LLM_JUDGE_TOTAL.labels(result="rejected").inc()
            continue

        group_key = f"auto_mf:{poly_id[:24]}"
        approved_by = "auto" if confidence >= AUTO_MATCH_LLM_THRESHOLD else "pending"

        await upsert_match(pool, {
            "group_key": group_key, "source": "polymarket",
            "source_id": poly_id, "side": "YES",
            "match_score": embed_score, "llm_confidence": confidence,
            "rule_notes": rule_diff, "approved_by": approved_by,
        })
        await upsert_match(pool, {
            "group_key": group_key, "source": "manifold",
            "source_id": manifold_slug, "side": "YES",
            "match_score": embed_score, "llm_confidence": confidence,
            "rule_notes": rule_diff, "approved_by": approved_by,
        })

        LLM_JUDGE_TOTAL.labels(result="approved" if approved_by == "auto" else "pending").inc()
        if approved_by == "auto":
            approved += 1
        log.info(
            "manifold_pair_found",
            poly=poly_id, manifold=manifold_slug,
            score=embed_score, confidence=confidence, approved_by=approved_by,
        )

    log.info("manifold_auto_match_done", approved=approved, total_candidates=len(candidates))


async def main() -> None:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )

    start_metrics_server(port=8003)

    pool = await get_pool()

    s = get_settings()
    kalshi_enabled = bool(s.kalshi_api_key_id and (s.kalshi_private_key_path or s.kalshi_private_key_b64))

    # Always load manual map first
    await load_manual_map(pool)

    scheduler = AsyncIOScheduler()

    if kalshi_enabled:
        scheduler.add_job(
            auto_match_polymarket_vs_kalshi,
            "interval",
            minutes=30,
            args=[pool],
            id="auto_match_kalshi",
        )

    # Auto-match Polymarket↔Manifold every 6 hours (500 markets × LLM latency)
    scheduler.add_job(
        auto_match_polymarket_vs_manifold,
        "interval",
        hours=6,
        args=[pool],
        id="auto_match_manifold",
    )
    scheduler.add_job(
        load_manual_map,
        "interval",
        minutes=5,
        args=[pool],
        id="manual_map_reload",
    )

    scheduler.start()

    # Delay startup auto-match by 5 min to avoid CPU spike during system boot
    await asyncio.sleep(300)
    if kalshi_enabled:
        await auto_match_polymarket_vs_kalshi(pool)
    await auto_match_polymarket_vs_manifold(pool)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
