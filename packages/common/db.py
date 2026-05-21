from __future__ import annotations

import json
import asyncpg
import structlog
from datetime import datetime
from typing import Any

from .settings import get_settings

log = structlog.get_logger()

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codecs so Python dicts are accepted for jsonb columns."""
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        dsn = settings.database_url.replace("+asyncpg", "")
        _pool = await asyncpg.create_pool(
            dsn, min_size=2, max_size=10, command_timeout=30, init=_init_conn
        )
        log.info("db_pool_created")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Upsert helpers ────────────────────────────────────────────────────────

async def upsert_market(pool: asyncpg.Pool, m: dict) -> None:
    await pool.execute(
        """
        INSERT INTO markets
          (market_id, source, condition_id, question, description, slug, tags,
           end_date, tick_size, min_order_size, fee_schedule, active, raw, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now())
        ON CONFLICT (market_id) DO UPDATE SET
          question=EXCLUDED.question, description=EXCLUDED.description,
          end_date=EXCLUDED.end_date, tick_size=EXCLUDED.tick_size,
          active=EXCLUDED.active, raw=EXCLUDED.raw, updated_at=now()
        """,
        m["market_id"], m["source"], m.get("condition_id"),
        m.get("question"), m.get("description"), m.get("slug"),
        m.get("tags", []), m.get("end_date"),
        m.get("tick_size"), m.get("min_order_size"),
        m.get("fee_schedule"), m.get("active", True),
        m.get("raw"),
    )


async def upsert_token(pool: asyncpg.Pool, token_id: str, market_id: str, outcome: str) -> None:
    await pool.execute(
        """
        INSERT INTO tokens (token_id, market_id, outcome)
        VALUES ($1, $2, $3)
        ON CONFLICT (token_id) DO NOTHING
        """,
        token_id, market_id, outcome,
    )


async def insert_price(pool: asyncpg.Pool, p: dict) -> None:
    await pool.execute(
        """
        INSERT INTO prices
          (ts, token_id, source, best_bid, best_ask, mid, last_trade,
           bid_size_top, ask_size_top, liquidity, volume_24h)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """,
        p["ts"], p["token_id"], p["source"],
        p.get("best_bid"), p.get("best_ask"), p.get("mid"),
        p.get("last_trade"), p.get("bid_size_top"), p.get("ask_size_top"),
        p.get("liquidity"), p.get("volume_24h"),
    )


async def insert_prices_bulk(pool: asyncpg.Pool, records: list[dict]) -> None:
    if not records:
        return
    await pool.executemany(
        """
        INSERT INTO prices
          (ts, token_id, source, best_bid, best_ask, mid, last_trade,
           bid_size_top, ask_size_top, liquidity, volume_24h)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """,
        [
            (
                r["ts"], r["token_id"], r["source"],
                r.get("best_bid"), r.get("best_ask"), r.get("mid"),
                r.get("last_trade"), r.get("bid_size_top"), r.get("ask_size_top"),
                r.get("liquidity"), r.get("volume_24h"),
            )
            for r in records
        ],
    )


async def insert_alert(pool: asyncpg.Pool, kind: str, group_key: str,
                       payload: dict, edge_bps: int) -> str:
    row = await pool.fetchrow(
        """
        INSERT INTO alerts (kind, group_key, payload, edge_bps)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        kind, group_key, payload, edge_bps,
    )
    return str(row["id"])


async def upsert_match(pool: asyncpg.Pool, m: dict) -> None:
    await pool.execute(
        """
        INSERT INTO market_matches
          (group_key, source, source_id, side, match_score, llm_confidence,
           rule_notes, approved_by, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now())
        ON CONFLICT (group_key, source) DO UPDATE SET
          source_id=EXCLUDED.source_id, side=EXCLUDED.side,
          match_score=EXCLUDED.match_score, llm_confidence=EXCLUDED.llm_confidence,
          rule_notes=EXCLUDED.rule_notes, approved_by=EXCLUDED.approved_by,
          updated_at=now()
        """,
        m["group_key"], m["source"], m["source_id"], m.get("side", "YES"),
        m.get("match_score"), m.get("llm_confidence"),
        m.get("rule_notes"), m.get("approved_by", "pending"),
    )


async def get_approved_matches(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT
          mm_a.group_key,
          mm_a.source_id  AS poly_token_yes,
          mm_b.source_id  AS kalshi_ticker,
          mm_a.side       AS poly_side,
          mm_b.side       AS kalshi_side,
          mm_a.rule_notes AS notes,
          m_a.question    AS title,
          m_a.slug        AS poly_slug,
          mm_b.source_id  AS kalshi_slug
        FROM market_matches mm_a
        JOIN market_matches mm_b ON mm_a.group_key = mm_b.group_key AND mm_b.source = 'kalshi'
        JOIN markets m_a ON m_a.market_id = mm_a.source_id
        WHERE mm_a.source = 'polymarket'
          AND mm_a.approved_by IN ('auto', 'manual')
          AND mm_b.approved_by IN ('auto', 'manual')
        """,
    )


async def is_muted(pool: asyncpg.Pool, group_key: str) -> bool:
    row = await pool.fetchrow(
        "SELECT muted_until FROM mutes WHERE group_key=$1 AND muted_until > now()",
        group_key,
    )
    return row is not None
