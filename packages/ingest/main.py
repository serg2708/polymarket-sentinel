"""Ingest service entry point.

Responsibilities:
1. Periodic Gamma poll → upsert markets + tokens to DB
2. WebSocket subscriber for top-N Polymarket markets → insert prices
3. Periodic Kalshi poll → insert prices
4. Periodic RSS + NewsAPI refresh → Redis news queues
5. Detect market_resolved WS events → label calibration snapshots
"""
from __future__ import annotations

import asyncio
import random
import signal
import time
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..common.db import get_pool, close_pool, upsert_market, upsert_token, insert_price, insert_prices_bulk
from ..common.metrics import (
    start_metrics_server,
    PRICE_TICKS_TOTAL,
    WS_RECONNECTS_TOTAL,
    WS_CONNECTED,
    ACTIVE_MARKETS,
    INGEST_LATENCY,
)
from ..common.settings import get_settings
from ..models.calibration import mark_resolved
from .polymarket import (
    fetch_active_events,
    fetch_active_markets,
    fetch_markets_by_ids,
    parse_market_record,
    extract_tokens,
    fetch_orderbook,
    book_to_price_record,
)
from .polymarket_ws import stream_market, parse_ws_book
from .kalshi import KalshiClient, parse_kalshi_market, kalshi_price_record
from .news import refresh_all_feeds, refresh_newsapi_for_markets

log = structlog.get_logger()

_tracked_token_ids: list[str] = []
_market_questions: list[tuple[str, str]] = []  # [(group_key, question), ...]


async def refresh_markets(pool: asyncpg.Pool) -> None:
    global _tracked_token_ids, _market_questions

    s = get_settings()
    # Fetch events (not raw markets) so each market gets parent event slug injected.
    # The /events endpoint returns event objects with embedded markets; we flatten
    # them and inject events=[{slug, title}] into each market dict so that
    # COALESCE(m.raw->'events'->0->>'slug', m.slug) in detectors returns the
    # parent event URL (e.g. "what-price-will-ethereum-hit-before-2027") instead
    # of the individual market slug ("will-ethereum-reach-6000-by-...").
    raw_events = await fetch_active_events(limit=500)
    event_counts: dict[str, int] = {}
    markets = []
    for ev in raw_events:
        ev_slug = ev.get("slug") or ""
        ev_title = ev.get("title") or ""
        for m in (ev.get("markets") or []):
            # Inject parent event reference so detectors can build correct URLs
            if not m.get("events"):
                m["events"] = [{"slug": ev_slug, "title": ev_title}]
            event_slug = ev_slug or m.get("slug") or ""
            count = event_counts.get(event_slug, 0)
            if event_slug and count >= s.max_markets_per_event:
                continue
            event_counts[event_slug] = count + 1
            markets.append(m)
            if len(markets) >= s.top_markets_by_volume:
                break
        if len(markets) >= s.top_markets_by_volume:
            break
    log.info("gamma_diversity_filter",
             fetched=sum(len(ev.get("markets") or []) for ev in raw_events),
             after_cap=len(markets),
             unique_events=len(event_counts))

    # Build market_id → event_slug mapping from all fetched events (used for supplemental lookup)
    market_to_event: dict[str, dict] = {}
    for ev in raw_events:
        ev_ref = {"slug": ev.get("slug") or "", "title": ev.get("title") or ""}
        for m in (ev.get("markets") or []):
            mid = str(m.get("id") or "")
            if mid:
                market_to_event[mid] = ev_ref

    # Supplemental: always track specific high-value markets regardless of volume rank.
    # Inject event slug from the in-memory mapping; fallback to /markets/{id} only.
    supp_ids = [i.strip() for i in s.supplemental_market_ids.split(",") if i.strip()]
    tracked_ids = {str(m.get("id") or "") for m in markets}
    supp_ids_new = [i for i in supp_ids if i not in tracked_ids]
    if supp_ids_new:
        supp_markets = await fetch_markets_by_ids(supp_ids_new)
        for m in supp_markets:
            mid = str(m.get("id") or "")
            if not m.get("events") and mid in market_to_event:
                m["events"] = [market_to_event[mid]]
        markets.extend(supp_markets)

    token_ids = []
    questions = []
    gamma_prices = []
    now = datetime.now(timezone.utc)
    for m in markets:
        rec = parse_market_record(m)
        await upsert_market(pool, rec)
        mkt_tokens = list(extract_tokens(m))
        for token_id, market_id, outcome in mkt_tokens:
            await upsert_token(pool, token_id, market_id, outcome)
            token_ids.append(token_id)
        q = m.get("question") or ""
        if q:
            questions.append((rec["market_id"], q))
        # Store Gamma AMM prices for markets without active CLOB books
        import json as _json
        _op = m.get("outcomePrices") or []
        outcome_prices = _json.loads(_op) if isinstance(_op, str) else _op
        if outcome_prices and len(outcome_prices) >= 2:
            try:
                for i, (token_id, _, _) in enumerate(mkt_tokens):
                    if i < len(outcome_prices):
                        mid = float(outcome_prices[i])
                        if 0.001 < mid < 0.999:
                            gamma_prices.append({
                                "token_id": token_id,
                                "ts": now,
                                "mid": mid,
                                "bid": mid,
                                "ask": mid,
                                "source": "gamma",
                            })
            except Exception:
                pass
    if gamma_prices:
        await insert_prices_bulk(pool, gamma_prices)
        log.debug("gamma_prices_stored", n=len(gamma_prices))

    _tracked_token_ids = token_ids
    _market_questions = questions
    ACTIVE_MARKETS.labels(source="polymarket").set(len(markets))
    log.info("gamma_refresh_done", n_markets=len(markets), n_tokens=len(token_ids),
             supplemental=len(supp_ids_new))


async def poly_ws_handler(pool: asyncpg.Pool, ev: dict) -> None:
    """Process a single Polymarket WS event."""
    # Handle market resolution
    if ev.get("event_type") == "market_resolved":
        market_id = ev.get("market_id") or ev.get("condition_id") or ev.get("asset_id", "")
        outcome_str = str(ev.get("outcome", "")).lower()
        if outcome_str in ("yes", "1", "true"):
            await mark_resolved(pool, market_id, 1)
        elif outcome_str in ("no", "0", "false"):
            await mark_resolved(pool, market_id, 0)
        # Mark market inactive in DB
        await pool.execute(
            "UPDATE markets SET active=false, updated_at=now() WHERE market_id=$1 OR condition_id=$1",
            market_id,
        )
        return

    record = parse_ws_book(ev)
    if record:
        t0 = time.monotonic()
        try:
            await insert_price(pool, record)
            PRICE_TICKS_TOTAL.labels(source="polymarket").inc()
        except Exception as exc:
            log.error("price_insert_error", error=str(exc))
        finally:
            INGEST_LATENCY.labels(source="polymarket").observe(time.monotonic() - t0)


async def run_poly_ws(pool: asyncpg.Pool) -> None:
    """Run the Polymarket WS subscription, tracking reconnect metrics."""
    while True:
        token_ids = _tracked_token_ids[:50]  # Polymarket WS cap per connection
        if not token_ids:
            await asyncio.sleep(5)
            continue
        try:
            WS_CONNECTED.set(1)
            await stream_market(
                token_ids,
                on_event=lambda ev: poly_ws_handler(pool, ev),
            )
        except Exception as exc:
            log.error("poly_ws_task_crash", error=str(exc))
        finally:
            WS_CONNECTED.set(0)
            WS_RECONNECTS_TOTAL.labels(venue="polymarket").inc()
        await asyncio.sleep(5)


async def refresh_kalshi_prices(pool: asyncpg.Pool) -> None:
    s = get_settings()
    if not s.kalshi_api_key_id or not (s.kalshi_private_key_path or s.kalshi_private_key_b64):
        log.debug("kalshi_skipped_no_credentials")
        return
    client = KalshiClient()
    try:
        markets = await client.get_markets(limit=100)
        for m in markets:
            await upsert_market(pool, parse_kalshi_market(m))
        ACTIVE_MARKETS.labels(source="kalshi").set(len(markets))

        for m in markets[:50]:
            ticker = m.get("ticker", "")
            t0 = time.monotonic()
            book = await client.get_orderbook(ticker, depth=5)
            if book:
                rec = kalshi_price_record(ticker, book)
                await insert_price(pool, rec)
                PRICE_TICKS_TOTAL.labels(source="kalshi").inc()
                INGEST_LATENCY.labels(source="kalshi").observe(time.monotonic() - t0)
            await asyncio.sleep(0.05)
    except Exception as exc:
        log.error("kalshi_refresh_error", error=str(exc))
    finally:
        await client.aclose()


async def poll_books_fallback(pool: asyncpg.Pool) -> None:
    """REST fallback for tracked tokens (safety net when WS drops)."""
    if not _tracked_token_ids:
        return
    sample = random.sample(_tracked_token_ids, min(20, len(_tracked_token_ids)))
    records = []
    for tid in sample:
        book = await fetch_orderbook(tid)
        if book:
            records.append(book_to_price_record(book, tid))
        await asyncio.sleep(0.1)
    if records:
        await insert_prices_bulk(pool, records)


async def refresh_news(redis_client) -> None:
    """Refresh RSS feeds and NewsAPI for watched markets.

    Picks markets that are mid-range priced (most informative for news signals)
    rather than the first 20 in the list, which are insertion-order arbitrary.
    """
    await refresh_all_feeds(redis_client)
    if _market_questions:
        # Prefer markets with uncertain prices (0.15–0.85) — news matters most there.
        # _market_questions is [(market_id, question), ...]; we don't have price here
        # so just shuffle to avoid always querying the same 20 markets.
        import random
        sample = random.sample(_market_questions, min(20, len(_market_questions)))
        await refresh_newsapi_for_markets(redis_client, sample)


async def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )

    start_metrics_server(port=8000)

    s = get_settings()
    pool = await get_pool()
    redis_client = aioredis.from_url(s.redis_url, decode_responses=True)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        refresh_markets,
        "interval",
        seconds=s.gamma_poll_interval_seconds,
        args=[pool],
        id="gamma_refresh",
        next_run_time=datetime.now(timezone.utc),
    )

    scheduler.add_job(
        refresh_kalshi_prices,
        "interval",
        seconds=60,
        args=[pool],
        id="kalshi_refresh",
        next_run_time=datetime.now(timezone.utc),
    )

    scheduler.add_job(
        refresh_news,
        "interval",
        hours=6,
        args=[redis_client],
        id="news_refresh",
    )

    scheduler.start()

    ws_task = asyncio.create_task(run_poly_ws(pool))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop_event.set())

    await stop_event.wait()

    scheduler.shutdown(wait=False)
    ws_task.cancel()
    await close_pool()
    await redis_client.aclose()
    log.info("ingest_stopped")


if __name__ == "__main__":
    asyncio.run(main())
