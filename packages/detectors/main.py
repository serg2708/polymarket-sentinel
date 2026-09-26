"""Detectors service entry point.

Detection cadences:
  - Arb (cross-platform + intra-market): every 5 s
  - Liquidity anomalies: every 5 s
  - Soft-edge (Metaculus + Manifold + PredictIt): every 60 s
  - News divergence: every 120 s (LLM-heavy)
  - LLM prior (Claude P(YES) estimate): every 4 h, cached 6 h per market
  - Calibration snapshots: every 3600 s (hourly)
"""
from __future__ import annotations

import asyncio
import decimal
import hashlib
import json
import signal
import time
import traceback
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
import structlog

from ..common.db import get_pool, close_pool, get_approved_matches, insert_alert, is_muted
from ..common.metrics import (
    start_metrics_server,
    ALERTS_TOTAL,
    DETECTION_LOOP_DURATION,
    ARB_EDGE_BPS,
    ALERT_QUEUE_DEPTH,
)
from ..common.settings import get_settings
from ..ingest.polymarket import fetch_orderbook, fetch_midpoints_batch
from ..ingest.kalshi import KalshiClient, kalshi_book_to_no_asks
from ..ingest.metaculus import get_question as get_metaculus_question
from ..ingest.manifold import get_market_by_slug as get_manifold_market
from ..ingest.predictit import get_contract as get_predictit_contract
from ..ingest.llm_prior import (
    estimate_probability as llm_estimate,
    estimate_tail_risk,
    is_multibracket_numeric,
    is_low_numeric_bracket,
    bracket_phantom_edge_reason,
    llm_backend_label,
    sibling_markets,
    _filter_news,
)
from ..models.calibration import snapshot_calibration as _snapshot_calibration, mark_resolved


async def snapshot_calibration(pool, redis_client, market_id: str,
                               model_p: float, market_p: float, model_kind: str) -> None:
    """Rate-limited wrapper: one snapshot per market per kind per day."""
    key = f"cal_snap:{model_kind}:{market_id}"
    if not await redis_client.set(key, "1", nx=True, ex=86400):
        return
    await _snapshot_calibration(pool, market_id=market_id,
                                model_p=model_p, market_p=market_p, model_kind=model_kind)
from .arb_xplatform import Leg, find_arb
from .arb_intramarket import find_intramarket_arb
from .soft_edge import metaculus_soft_edge, manifold_soft_edge, predictit_soft_edge, llm_prior_soft_edge
from .news_divergence import run_news_divergence_check

log = structlog.get_logger()
settings = get_settings()

ALERT_QUEUE_KEY = "polysentinel:alerts"

# How long to suppress repeat alerts of the same (kind, group_key, edge_bucket).
# Soft-edge signals from slow-moving sources need a long window; arb can repeat quickly.
_DEDUP_TTL: dict[str, int] = {
    "arb_xplatform":      1800,   # 30 min — arb can close fast
    "arb_intramarket":    1800,
    "soft_edge_manifold": 14400,  # 4 h — Manifold prices barely change hourly
    "soft_edge_metaculus":14400,
    "soft_edge_predictit":7200,   # 2 h
    "soft_edge_llm_prior":21600,  # 6 h
    "tail_risk":          21600,  # 6 h
    "news_divergence":    43200,  # 12 h
}
_DEDUP_TTL_DEFAULT = 1800

# Edge bucket granularity per kind.  Arb/spike signals use fine buckets (50 bps)
# because the exact price level matters.  Fundamental signals (soft edge, tail
# risk, news) use 0 — meaning edge_bps is excluded from the dedup key entirely
# so that daily price drift doesn't create new keys for the same opportunity.
_EDGE_BUCKET: dict[str, int] = {
    "arb_xplatform":      50,
    "arb_intramarket":    50,
    # Everything else defaults to 0 → no edge component in dedup key
}


def _has_rule_diff(rule_notes: str | None) -> bool:
    """Return True if notes document a known rule difference between the two markets.
    Such pairs are useful for calibration but the price gap is NOT a trading signal."""
    if not rule_notes:
        return False
    lower = rule_notes.lower()
    return "rule diff" in lower or "differ" in lower or "different scenario" in lower


def _json_default(obj):
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


# ── Blocked markets ───────────────────────────────────────────────────────

def _blocked_ids() -> set[str]:
    ids = settings.blocked_market_ids or ""
    return {i.strip() for i in ids.split(",") if i.strip()}


# ── Deduplication ─────────────────────────────────────────────────────────

def _dedup_key(kind: str, group_key: str, edge_bucket: int) -> str:
    # bucket=0 means edge is not part of the key (fundamental signals)
    raw = f"{kind}:{group_key}" if edge_bucket == 0 else f"{kind}:{group_key}:{edge_bucket}"
    return "dedup:" + hashlib.sha1(raw.encode()).hexdigest()[:16]


async def should_send(redis_client, kind: str, group_key: str, edge_bps: int) -> bool:
    bucket = _EDGE_BUCKET.get(kind, 0)
    key = _dedup_key(kind, group_key, edge_bps // bucket * bucket if bucket else 0)
    ttl = _DEDUP_TTL.get(kind, _DEDUP_TTL_DEFAULT)
    result = await redis_client.set(key, "1", nx=True, ex=ttl)
    return bool(result)


async def push_alert(redis_client, alert: dict) -> None:
    await redis_client.rpush(ALERT_QUEUE_KEY, json.dumps(alert, default=_json_default))
    ALERTS_TOTAL.labels(kind=alert.get("kind", "unknown")).inc()
    if alert.get("edge_bps"):
        ARB_EDGE_BPS.labels(kind=alert.get("kind", "unknown")).observe(alert["edge_bps"])


# ── Cross-platform arb ────────────────────────────────────────────────────

async def run_arb_detection(
    pool: asyncpg.Pool,
    redis_client,
    kalshi_client: KalshiClient,
) -> None:
    matches = await get_approved_matches(pool)
    if not matches:
        return

    for m in matches:
        group_key = m["group_key"]
        if await is_muted(pool, group_key):
            continue

        poly_token = m["poly_token_yes"]
        kalshi_ticker = (m["kalshi_ticker"] or "").replace("kalshi:", "")

        poly_book_raw, kalshi_book_raw = await asyncio.gather(
            fetch_orderbook(poly_token),
            kalshi_client.get_orderbook(kalshi_ticker, depth=10),
            return_exceptions=True,
        )

        if isinstance(poly_book_raw, Exception) or poly_book_raw is None:
            continue
        if isinstance(kalshi_book_raw, Exception) or kalshi_book_raw is None:
            continue

        poly_asks = [
            (float(a["price"]), float(a["size"]))
            for a in sorted(poly_book_raw.get("asks", []), key=lambda x: float(x["price"]))
        ]
        kalshi_no_asks = kalshi_book_to_no_asks(kalshi_book_raw)

        if not poly_asks or not kalshi_no_asks:
            continue

        result = find_arb(
            Leg("polymarket", poly_token, "YES", poly_asks, taker_fee_bps=0),
            Leg("kalshi", kalshi_ticker, "NO", kalshi_no_asks, taker_fee_bps=100),
            min_edge_bps=settings.arb_min_edge_bps,
        )

        if result and await should_send(redis_client, "arb_xplatform", group_key, result["edge_bps"]):
            alert = {
                "kind": "arb_xplatform",
                "group_key": group_key,
                "title": m.get("title", group_key),
                "poly_url": f"https://polymarket.com/event/{m.get('poly_slug', '')}",
                "kalshi_url": f"https://kalshi.com/markets/{kalshi_ticker}",
                **result,
            }
            await push_alert(redis_client, alert)
            await insert_alert(pool, "arb_xplatform", group_key, alert, result["edge_bps"])
            log.info("arb_xplatform_alert", group_key=group_key, edge_bps=result["edge_bps"])


# ── Intra-market arb ──────────────────────────────────────────────────────

async def run_intramarket_detection(pool: asyncpg.Pool, redis_client) -> None:
    tokens = await pool.fetch(
        """
        SELECT t.token_id, t.market_id, t.outcome, m.question,
               COALESCE(m.raw->'events'->0->>'slug', m.slug) AS event_slug
        FROM tokens t
        JOIN markets m ON m.market_id = t.market_id
        WHERE m.active = true AND m.source = 'polymarket'
        ORDER BY m.updated_at DESC
        LIMIT 100
        """
    )

    by_market: dict[str, dict] = {}
    for t in tokens:
        mid = t["market_id"]
        if mid not in by_market:
            by_market[mid] = {"question": t["question"], "event_slug": t["event_slug"], "YES": None, "NO": None}
        by_market[mid][t["outcome"]] = t["token_id"]

    for market_id, info in by_market.items():
        yes_token = info.get("YES")
        no_token = info.get("NO")
        if not yes_token or not no_token:
            continue

        yes_book, no_book = await asyncio.gather(
            fetch_orderbook(yes_token),
            fetch_orderbook(no_token),
            return_exceptions=True,
        )
        if isinstance(yes_book, Exception) or yes_book is None:
            continue
        if isinstance(no_book, Exception) or no_book is None:
            continue

        def _asks(book): return [
            (float(a["price"]), float(a["size"]))
            for a in sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        ]

        result = find_intramarket_arb(_asks(yes_book), _asks(no_book))
        if result:
            group_key = f"intra:{market_id[:16]}"
            if await should_send(redis_client, "arb_intramarket", group_key, result["edge_bps"]):
                alert = {
                    **result,
                    "group_key": group_key,
                    "title": info.get("question", market_id),
                    "poly_url": f"https://polymarket.com/event/{info.get('event_slug', '')}",
                }
                await push_alert(redis_client, alert)
                await insert_alert(pool, "arb_intramarket", group_key, alert, result["edge_bps"])
                log.info("intra_arb_alert", group_key=group_key, edge_bps=result["edge_bps"])


# ── Soft-edge (Metaculus + Manifold vs Polymarket) ────────────────────────

async def run_soft_edge_detection(pool: asyncpg.Pool, redis_client) -> None:
    """
    For each approved match that also has a metaculus/manifold leg, compare
    those probabilities against the current Polymarket ask price.
    """
    # Fetch matches that have metaculus or manifold legs
    meta_matches = await pool.fetch(
        """
        SELECT
          mm_poly.group_key,
          t.token_id          AS poly_token,
          mm_meta.source_id   AS meta_id,
          mm_meta.source      AS meta_source,
          mm_poly.approved_by AS approved_by,
          COALESCE(mm_meta.rule_notes, mm_poly.rule_notes) AS rule_notes,
          m.question,
          COALESCE(m.raw->'events'->0->>'slug', m.slug) AS event_slug
        FROM market_matches mm_poly
        JOIN market_matches mm_meta
          ON mm_poly.group_key = mm_meta.group_key
          AND mm_meta.source IN ('metaculus', 'manifold', 'predictit')
        JOIN markets m ON m.market_id = mm_poly.source_id
        JOIN tokens t ON t.market_id = mm_poly.source_id AND t.outcome = 'Yes'
        WHERE mm_poly.source = 'polymarket'
          AND mm_poly.approved_by IN ('auto', 'manual')
        LIMIT 50
        """
    )

    for m in meta_matches:
        group_key = m["group_key"]

        # Get current Polymarket mid price — DB first, REST fallback
        price_row = await pool.fetchrow(
            """
            SELECT mid FROM prices
            WHERE token_id = $1 AND source = 'polymarket'
            ORDER BY ts DESC LIMIT 1
            """,
            m["poly_token"],
        )
        if price_row and price_row["mid"] is not None:
            poly_ask = float(price_row["mid"])
        else:
            book = await fetch_orderbook(m["poly_token"])
            if not book:
                continue
            asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 1)))
            bids = sorted(book.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            if best_bid is None or best_ask is None:
                continue
            poly_ask = (best_bid + best_ask) / 2

        min_mid = settings.liquidity_min_mid
        if poly_ask < min_mid or poly_ask > (1.0 - min_mid):
            continue

        model_p: float | None = None
        hit = None

        if m["meta_source"] == "metaculus":
            try:
                qid = int(m["meta_id"])
                meta = await get_metaculus_question(qid)
            except (ValueError, TypeError):
                continue
            if not meta or meta.get("community_median") is None:
                continue
            model_p = float(meta["community_median"])
            hit = metaculus_soft_edge(model_p, poly_ask, min_edge_bps=settings.soft_edge_min_bps, min_edge_pp=settings.soft_edge_min_pp)
            model_kind = "metaculus"

        elif m["meta_source"] == "manifold":
            mkt = await get_manifold_market(m["meta_id"])
            if not mkt:
                continue
            raw_p = mkt.get("probability") or mkt.get("implied_prob")
            if raw_p is None:
                continue
            model_p = float(raw_p)
            hit = manifold_soft_edge(model_p, poly_ask, min_edge_bps=settings.soft_edge_min_bps, min_edge_pp=settings.soft_edge_min_pp)
            model_kind = "manifold"

        elif m["meta_source"] == "predictit":
            pi = await get_predictit_contract(m["meta_id"])
            if not pi:
                continue
            raw_p = pi.get("implied_prob")
            if raw_p is None:
                continue
            model_p = float(raw_p)
            # Real-money source: lower threshold than Manifold (300 bps, 3pp)
            hit = predictit_soft_edge(model_p, poly_ask, min_edge_bps=300, min_edge_pp=3.0)
            model_kind = "predictit"

        else:
            continue

        # Suppress alerts where the gap is explained by a known rule difference.
        if hit and _has_rule_diff(m.get("rule_notes")):
            log.debug("soft_edge_suppressed_rule_diff", group_key=group_key)
            hit = None

        if hit:
            if await should_send(redis_client, hit["kind"], group_key, int(hit.get("ev_per_dollar", 0) * 10000)):
                event_slug = m.get("event_slug") or ""
                alert = {
                    **hit,
                    "group_key": group_key,
                    "title": m.get("question", group_key),
                    "poly_url": f"https://polymarket.com/event/{event_slug}" if event_slug else None,
                    "rule_notes": m.get("rule_notes"),
                    "approved_by": m.get("approved_by"),
                }
                await push_alert(redis_client, alert)
                await insert_alert(pool, hit["kind"], group_key, alert, int(abs(hit.get("ev_per_dollar", 0)) * 10000))
                log.info("soft_edge_alert", group_key=group_key, source=m["meta_source"])

        # Snapshot for calibration once per day per market
        if model_p is not None:
            await snapshot_calibration(
                pool, redis_client,
                market_id=m["poly_token"],
                model_p=model_p,
                market_p=poly_ask,
                model_kind=model_kind,
            )


# ── LLM prior (Claude probability estimate vs Polymarket) ────────────────

_SPORTS_KEYWORDS = frozenset([
    "nba", "nhl", "nfl", "mlb", "nascar", "mls",
    "stanley cup", "super bowl", "world series",
    "playoffs", "playoff",
    "premier league", "la liga", "serie a",
    "bundesliga", "ucl",
    # Removed: "win the", "beat the", "defeat the", "world cup", "champion",
    # "championship", "finals", "uefa" — too broad, blocks Eurovision, Oscars,
    # Nobel prize, geopolitical events ("champion of democracy", etc.)
])

_GEOPOLITICAL_MARKERS = frozenset([
    "assassinat", "eliminat", "killed", "coup", "overthrow", "impeach",
    "resign", "invade", "invasion", "ceasefire", "nuclear", "sanction",
])


def _is_sports_market(question: str) -> bool:
    """True when the market is a live-score sport result Claude has no post-cutoff data on.

    Does NOT exclude tournament winner markets (Eurovision, Oscars, World Cup champion)
    because news injection can still provide meaningful signal. Only excludes
    league-specific markets where the outcome is purely a live game result.
    Geopolitical markets are never excluded regardless of phrasing.
    """
    q = question.lower()
    # Geopolitical events always pass through — high-value tail risk candidates
    if any(m in q for m in _GEOPOLITICAL_MARKERS):
        return False
    return any(kw in q for kw in _SPORTS_KEYWORDS)


async def run_llm_prior_detection(pool: asyncpg.Pool, redis_client) -> None:
    """Ask Claude to estimate P(YES) for high-volume Polymarket markets.

    Runs every 4 hours (cadence set in main loop). Results are cached in
    Redis for 6 hours per market, so actual API calls are infrequent.
    Only fires when edge >= soft_edge_min_bps and gap >= soft_edge_min_pp
    AND Claude's confidence >= 0.75 (guards against stale training data).
    Sports/live-score markets are skipped entirely (post-cutoff blindspot).
    """
    from ..ingest.news import pop_recent_articles

    api_key = settings.anthropic_api_key
    if not api_key:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Top markets by volume with mid price in range [min_mid, 1-min_mid]
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (m.market_id)
          m.market_id,
          m.question,
          m.description,
          COALESCE(m.raw->'events'->0->>'slug', m.slug) AS event_slug,
          p.mid AS poly_mid
        FROM markets m
        JOIN tokens t ON t.market_id = m.market_id AND t.outcome = 'Yes'
        JOIN prices p ON p.token_id = t.token_id
        WHERE m.source = 'polymarket'
          AND m.active = true
          AND p.ts > now() - INTERVAL '6 hours'
          AND p.mid BETWEEN $1 AND $2
          AND (m.raw->>'volume')::float > 20000
        ORDER BY m.market_id, p.ts DESC
        LIMIT 120
        """,
        settings.liquidity_min_mid,
        1.0 - settings.liquidity_min_mid,
    )

    blocked = _blocked_ids()
    for row in rows:
        market_id = row["market_id"]
        if str(market_id) in blocked:
            continue
        question = row["question"] or market_id
        poly_ask = float(row["poly_mid"])

        # Sports results are post-cutoff blindspots — skip entirely
        if _is_sports_market(question):
            continue

        # Pull recent news. Require a fresh catalyst: an LLM prior that disagrees
        # with a liquid market WITHOUT news is a blind guess (cf. MegaETH, where
        # the LLM said 25-30% vs a $1.5M market at 14% and the market was right).
        news_key = f"market:{market_id}"
        news = await pop_recent_articles(redis_client, news_key, n=6)
        fresh_news = _filter_news(news)
        if len(fresh_news) < 2:
            log.debug("llm_prior_skip_no_news", market_id=market_id,
                      fresh_news=len(fresh_news))
            continue

        result = await llm_estimate(
            market_id=market_id,
            question=question,
            description=row["description"] or "",
            redis_client=redis_client,
            today=today,
            recent_news=news,
        )
        if result is None:
            continue
        llm_p, confidence = result

        # Require meaningful confidence — 0.70 still guards against pure guessing
        if confidence < 0.70:
            log.debug("llm_prior_low_confidence", market_id=market_id, confidence=confidence)
            continue

        # Sanity gate: on the lowest numeric bracket of a multi-outcome market
        # ("< $X" with a separate "No event" outcome), a prior ABOVE market means
        # BUY YES — almost always the LLM folding "event won't happen" into the
        # low bracket. Suppress that direction; a prior below market (BUY NO) is fine.
        description = row["description"] or ""
        bracket_reason = (bracket_phantom_edge_reason(question, description)
                          if llm_p > poly_ask else None)
        if bracket_reason:
            log.warning("llm_prior_bracket_suppressed", market_id=market_id,
                        question=question[:80], llm_p=round(llm_p, 3),
                        poly_ask=round(poly_ask, 3),
                        reason=f"{bracket_reason}; manual review")
            continue

        hit = llm_prior_soft_edge(
            llm_p, poly_ask,
            min_edge_bps=settings.soft_edge_min_bps,
            min_edge_pp=10.0,  # require ≥10 pp gap for LLM-only signals
        )
        if not hit:
            continue

        group_key = f"llm_prior:{market_id[:20]}"
        if await should_send(redis_client, hit["kind"], group_key, int(hit.get("ev_per_dollar", 0) * 10000)):
            event_slug = row.get("event_slug") or ""
            alert = {
                **hit,
                "group_key": group_key,
                "title": question,
                "poly_url": f"https://polymarket.com/event/{event_slug}" if event_slug else None,
            }
            await push_alert(redis_client, alert)
            await insert_alert(pool, hit["kind"], group_key, alert, int(abs(hit.get("ev_per_dollar", 0)) * 10000))
            log.info("llm_prior_alert", market_id=market_id, llm_p=llm_p, poly_ask=poly_ask,
                     confidence=confidence)

        await snapshot_calibration(
            pool, redis_client,
            market_id=market_id,
            model_p=llm_p,
            market_p=poly_ask,
            model_kind="llm_prior",
        )


# ── News divergence ───────────────────────────────────────────────────────

async def run_news_detection(pool: asyncpg.Pool, redis_client) -> None:
    """Run news-divergence check for approved matches that have recent articles."""
    matches = await pool.fetch(
        """
        SELECT
          mm.group_key,
          mm.source_id AS market_id,
          m.question,
          COALESCE(m.raw->'events'->0->>'slug', m.slug) AS event_slug,
          p.mid AS poly_mid
        FROM market_matches mm
        JOIN markets m ON m.market_id = mm.source_id AND mm.source = 'polymarket'
        LEFT JOIN LATERAL (
          SELECT mid FROM prices
          WHERE token_id IN (
            SELECT token_id FROM tokens WHERE market_id = mm.source_id AND outcome = 'Yes'
          )
          ORDER BY ts DESC LIMIT 1
        ) p ON true
        WHERE mm.approved_by IN ('auto','manual')
        LIMIT 30
        """
    )

    blocked = _blocked_ids()
    for row in matches:
        group_key = row["group_key"]
        market_id = row["market_id"]
        if str(market_id) in blocked:
            continue
        question = row["question"] or group_key

        if _is_sports_market(question):
            continue
        market_p = float(row["poly_mid"]) if row["poly_mid"] else 0.5
        event_slug = row["event_slug"] or ""

        # News is stored under the Polymarket market_id, not the group_key
        result = await run_news_divergence_check(
            redis_client,
            group_key=group_key,
            question=question,
            market_p=market_p,
            topic=f"market:{market_id}",
        )

        if result:
            result["poly_url"] = f"https://polymarket.com/event/{event_slug}" if event_slug else None

        if result and float(result.get("confidence") or 0) < 0.50:
            log.debug("news_divergence_low_confidence", group_key=group_key,
                      confidence=result.get("confidence"))
            continue

        if result and await should_send(redis_client, "news_divergence", group_key, result.get("edge_bps", 0)):
            yes_token_id = await pool.fetchval(
                "SELECT token_id FROM tokens WHERE market_id = $1 AND outcome = 'Yes' LIMIT 1",
                market_id,
            )
            result["market_id"] = market_id
            result["yes_token_id"] = yes_token_id
            await push_alert(redis_client, result)
            await insert_alert(pool, "news_divergence", group_key, result, result.get("edge_bps", 0))
            log.info("news_divergence_alert", group_key=group_key, sentiment=result.get("sentiment_ema"))


# ── Calibration snapshot (hourly) ─────────────────────────────────────────

async def run_calibration_snapshots(pool: asyncpg.Pool, redis_client) -> None:
    """Snapshot market_p for all tracked tokens once per hour."""
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (token_id)
          token_id, mid
        FROM prices
        WHERE source = 'polymarket' AND ts > now() - INTERVAL '10 minutes'
        ORDER BY token_id, ts DESC
        LIMIT 500
        """
    )
    for r in rows:
        if r["mid"] is not None:
            await snapshot_calibration(
                pool, redis_client,
                market_id=r["token_id"],
                model_p=float(r["mid"]),
                market_p=float(r["mid"]),
                model_kind="polymarket_baseline",
            )

    # Check for newly resolved markets (outcome field populated in Gamma)
    resolved = await pool.fetch(
        """
        SELECT market_id FROM markets
        WHERE active = false AND source = 'polymarket'
          AND updated_at > now() - INTERVAL '2 hours'
        """
    )
    for row in resolved:
        # We can't know YES/NO outcome without reading Gamma description — mark placeholder
        # (the ingest service should call mark_resolved when it sees market_resolved WS events)
        pass

    log.debug("calibration_snapshots_done", n=len(rows))


# ── Tail-risk underpricing detector ──────────────────────────────────────────

async def run_tail_risk_detection(pool: asyncpg.Pool, redis_client) -> None:
    """Scan low-probability markets (5–25%) for underpriced tail risks.

    For each candidate: pull recent news, ask Claude if the event is more
    likely than the market thinks, and check for confirming order flow.
    Fires only when Claude has specific news evidence AND confidence ≥ 0.80.
    Runs every 2 hours so fresh news always gets evaluated.
    """
    from ..ingest.news import pop_recent_articles, refresh_newsapi_for_markets
    from .arb_xplatform import kelly_fraction

    if not settings.anthropic_api_key:
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Low-probability markets with enough liquidity to be worth betting
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (m.market_id)
          m.market_id,
          m.question,
          m.description,
          COALESCE(m.raw->'events'->0->>'slug', m.slug) AS event_slug,
          p.mid AS poly_mid
        FROM markets m
        JOIN tokens t ON t.market_id = m.market_id AND t.outcome = 'Yes'
        JOIN prices p ON p.token_id = t.token_id
        WHERE m.source = 'polymarket'
          AND m.active = true
          AND p.ts > now() - INTERVAL '6 hours'
          AND p.mid BETWEEN 0.03 AND 0.35
          AND (m.raw->>'volume')::float > 20000
        ORDER BY m.market_id, p.ts DESC
        LIMIT 100
        """,
    )

    if not rows:
        return

    # Fetch fresh news specifically for these tail-risk candidates before evaluating
    market_questions = [(r["market_id"], r["question"] or r["market_id"]) for r in rows]
    try:
        await refresh_newsapi_for_markets(redis_client, market_questions, days_back=5)
    except Exception as exc:
        log.warning("tail_risk_news_refresh_error", error=str(exc))

    # Also pull background world/geopolitics/entertainment headlines for all evaluations
    world_news = await pop_recent_articles(redis_client, "world", n=15)
    geo_news = await pop_recent_articles(redis_client, "geopolitics", n=10)
    entertainment_news = await pop_recent_articles(redis_client, "entertainment", n=10)
    background_news = {a["url"]: a for a in world_news + geo_news + entertainment_news}  # dedup by url

    log.info("tail_risk_scan_start", n_markets=len(rows))
    blocked = _blocked_ids()
    fired = 0

    for row in rows:
        market_id = row["market_id"]
        if str(market_id) in blocked:
            continue
        question = row["question"] or market_id
        poly_p = float(row["poly_mid"])

        # Pull market-specific news first, supplement with background headlines
        market_news = await pop_recent_articles(redis_client, f"market:{market_id}", n=8)
        fresh_market_news = _filter_news(market_news)

        # Tail risk requires a concrete, market-specific news catalyst — for ANY
        # market at ANY confidence. Every losing signal (MegaETH, NATO-Russia,
        # Israeli Knesset) fired WITHOUT one: the LLM produced a phantom edge
        # against a liquid market on vague background headlines, and the LLM's
        # self-reported confidence (even 0.80) was not a reliable substitute. The
        # one winner (Knicks 8.8¢→78¢) had market-specific playoff news. No
        # catalyst → skip before spending an LLM call.
        if len(fresh_market_news) < 2:
            log.info("tail_risk_skip_no_catalyst", market_id=market_id,
                     fresh_market_news=len(fresh_market_news), question=question[:80])
            continue

        if len(market_news) < 3:
            extra = list(background_news.values())[:max(0, 6 - len(market_news))]
            news = market_news + extra
        else:
            news = market_news
        log.debug("tail_risk_news", market_id=market_id, specific=len(market_news), total=len(news))

        siblings_block, siblings_no = await sibling_markets(row.get("event_slug") or "", market_id)

        result = await estimate_tail_risk(
            market_id=market_id,
            question=question,
            description=row["description"] or "",
            market_p=poly_p,
            redis_client=redis_client,
            today=today,
            recent_news=news,
            siblings_block=siblings_block,
        )
        if result is None:
            continue

        claude_p, confidence, underpriced, key_signal = result

        # Must be explicitly flagged as underpriced with meaningful confidence.
        if not underpriced or confidence < 0.70:
            log.debug("tail_risk_skip", market_id=market_id,
                      underpriced=underpriced, confidence=confidence)
            continue

        # Claude's estimate must be meaningfully above market (at least 1.25×)
        # and at least 3 percentage points above to filter noise
        edge_pp_check = (claude_p - poly_p) * 100
        if claude_p < poly_p * 1.25 or edge_pp_check < 3.0:
            continue

        edge_pp = (claude_p - poly_p) * 100

        # An earlier-deadline market of this event already resolved NO, yet the model
        # sees a big edge: the market had the same news and it didn't meet the criteria
        # (Saudi East-West: "restarted, sources say" → Sep 22 resolved NO → phantom
        # 49pp signal on Sep 30). A liquid market doesn't leave that much on the table.
        if siblings_no and edge_pp > 25:
            log.warning("tail_risk_sibling_resolved_no_suppressed", market_id=market_id,
                        question=question[:80], claude_p=round(claude_p, 3), edge_pp=round(edge_pp, 1),
                        sibling=siblings_no[0]["question"][:80])
            continue

        ev = (claude_p - poly_p) / poly_p
        kf = kelly_fraction(claude_p, poly_p) * 0.5  # halve Kelly: Claude estimate, not certainty

        group_key = f"tail:{market_id[:20]}"
        edge_bps = int(ev * 10000)

        if await should_send(redis_client, "tail_risk", group_key, edge_bps):
            event_slug = row.get("event_slug") or ""
            yes_token_id = await pool.fetchval(
                "SELECT token_id FROM tokens WHERE market_id = $1 AND outcome = 'Yes' LIMIT 1",
                market_id,
            )
            alert = {
                "kind": "tail_risk",
                "group_key": group_key,
                "title": question,
                "market_id": market_id,
                "yes_token_id": yes_token_id,
                "market_p": round(poly_p, 4),
                "claude_p": round(claude_p, 4),
                "confidence": round(confidence, 2),
                "key_signal": key_signal,
                "model": llm_backend_label(),
                "edge_pp": round(edge_pp, 1),
                "ev_per_dollar": round(ev, 4),
                "kelly_fraction": round(kf, 4),
                "poly_url": f"https://polymarket.com/event/{event_slug}" if event_slug else None,
            }
            await push_alert(redis_client, alert)
            await insert_alert(pool, "tail_risk", group_key, alert, edge_bps)
            log.info("tail_risk_alert", market_id=market_id,
                     market_p=poly_p, claude_p=claude_p,
                     confidence=confidence, edge_pp=round(edge_pp, 1))
            fired += 1

        await snapshot_calibration(pool, redis_client,
                                   market_id=market_id,
                                   model_p=claude_p, market_p=poly_p,
                                   model_kind="tail_risk")

    log.info("tail_risk_scan_done", scanned=len(rows), fired=fired)


# ── Alert queue depth gauge ───────────────────────────────────────────────

async def update_queue_gauge(redis_client) -> None:
    try:
        depth = await redis_client.llen(ALERT_QUEUE_KEY)
        ALERT_QUEUE_DEPTH.set(depth)
    except Exception:
        pass


# ── Main detection loop ───────────────────────────────────────────────────

async def detection_loop(pool: asyncpg.Pool, redis_client) -> None:
    s = get_settings()
    kalshi_client: KalshiClient | None = None
    if s.kalshi_api_key_id and (s.kalshi_private_key_path or s.kalshi_private_key_b64):
        kalshi_client = KalshiClient()
        log.info("kalshi_enabled")
    else:
        log.info("kalshi_disabled", reason="no API key configured — skipping cross-platform arb")

    tick = 0

    try:
        while True:
            t0 = time.monotonic()
            try:
                # Fast path: arb every tick (5s)
                fast_tasks = [
                    run_intramarket_detection(pool, redis_client),
                    update_queue_gauge(redis_client),
                ]
                if kalshi_client:
                    fast_tasks.append(run_arb_detection(pool, redis_client, kalshi_client))
                await asyncio.gather(*fast_tasks)

                # soft_edge disabled — saves Metaculus/Manifold/Claude API calls
                # if tick % 12 == 0:
                #     await run_soft_edge_detection(pool, redis_client)

                # Slow path: news divergence every 24 ticks (~120s)
                if tick % 24 == 0:
                    await run_news_detection(pool, redis_client)

                # Every 2 hours: llm_prior — Claude estimates vs market price (full range)
                if tick % 1440 == 0:
                    await run_llm_prior_detection(pool, redis_client)

                # Every 1 hour: tail-risk scan (low-prob markets 3–35%)
                if tick % 720 == 0:
                    await run_tail_risk_detection(pool, redis_client)

                # Hourly: calibration snapshots
                if tick % 720 == 0:
                    await run_calibration_snapshots(pool, redis_client)

            except Exception as exc:
                log.error("detection_loop_error", error=str(exc), tick=tick,
                          traceback=traceback.format_exc())

            elapsed = time.monotonic() - t0
            DETECTION_LOOP_DURATION.observe(elapsed)
            tick += 1

            await asyncio.sleep(max(0, 5.0 - elapsed))  # target 5s cadence

    finally:
        await kalshi_client.aclose()


async def main() -> None:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )

    start_metrics_server(port=8001)

    pool = await get_pool()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    detect_task = asyncio.create_task(detection_loop(pool, redis_client))

    await stop_event.wait()
    detect_task.cancel()
    await close_pool()
    await redis_client.aclose()
    log.info("detectors_stopped")


if __name__ == "__main__":
    asyncio.run(main())
