"""Polymarket Gamma API poller + CLOB REST helpers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30, http2=True, follow_redirects=True)
    return _http


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30))
async def fetch_active_events(limit: int = 500) -> list[dict]:
    """Paginate all live events from Gamma.

    The Gamma API caps per-page results at 100 regardless of the requested limit,
    so we paginate with page_size=100 until an empty page signals end-of-results.
    """
    c = _client()
    out, offset = [], 0
    page_size = 100  # Gamma API hard cap per request
    while len(out) < limit:
        r = await c.get(
            f"{GAMMA}/events",
            params={
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "volume_24hr",
                "ascending": "false",
            },
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        offset += len(page)
        if len(page) < page_size:
            break  # last page — fewer results than requested
        await asyncio.sleep(0.25)  # be polite to Cloudflare
    log.info("gamma_events_fetched", count=len(out))
    return out


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30))
async def fetch_active_markets(limit: int = 300, top_n: int = 500) -> list[dict]:
    """Paginate active markets, sorted by 24h volume."""
    c = _client()
    out, offset = [], 0
    while len(out) < top_n:
        r = await c.get(
            f"{GAMMA}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": min(limit, top_n - len(out)),
                "offset": offset,
                "order": "volume_24hr",
                "ascending": "false",
            },
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        offset += len(page)
        if len(page) < limit:
            break
        await asyncio.sleep(0.25)
    log.info("gamma_markets_fetched", count=len(out))
    return out


def parse_market_record(m: dict, source: str = "polymarket") -> dict:
    """Normalise a Gamma market dict into our DB schema."""
    # Gamma uses orderPriceMinTickSize / feeSchedule fields
    fee = None
    if m.get("takerBaseFee") is not None or m.get("makerBaseFee") is not None:
        fee = {"taker_bps": _float(m.get("takerBaseFee")), "maker_bps": _float(m.get("makerBaseFee"))}
    market_id = str(m.get("id") or m.get("conditionId") or "")
    return {
        "market_id": market_id,
        "source": source,
        "condition_id": m.get("conditionId"),
        "question": m.get("question"),
        "description": m.get("description"),
        "slug": m.get("slug"),
        "tags": [t.get("label", "") for t in (m.get("tags") or [])],
        "end_date": _parse_dt(m.get("endDate") or m.get("endDateIso")),
        "tick_size": _float(m.get("orderPriceMinTickSize") or m.get("minimum_tick_size")),
        "min_order_size": _float(m.get("orderMinSize") or m.get("minimum_order_size")),
        "fee_schedule": fee,
        "active": not m.get("closed", False) and m.get("active", True),
        "raw": m,
    }


def extract_tokens(m: dict) -> list[tuple[str, str, str]]:
    """Return [(token_id, market_id, outcome), ...] from a Gamma market dict.

    Gamma returns token IDs in clobTokenIds (JSON string: [yes_id, no_id])
    and outcomes in outcomes (JSON string: ["Yes","No"]).
    """
    import json as _json

    market_id = str(m.get("id") or m.get("conditionId") or "")

    # --- Gamma format: clobTokenIds is a JSON string ---
    clob_raw = m.get("clobTokenIds")
    if clob_raw:
        try:
            token_ids = _json.loads(clob_raw) if isinstance(clob_raw, str) else list(clob_raw)
        except Exception:
            token_ids = []
        outcomes_raw = m.get("outcomes")
        try:
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw or [])
        except Exception:
            outcomes = ["YES", "NO"]
        if len(outcomes) < len(token_ids):
            outcomes = outcomes + ["YES", "NO"][len(outcomes):]
        return [(str(tid), market_id, str(out)) for tid, out in zip(token_ids, outcomes) if tid]

    # --- CLOB format: tokens is a list of dicts ---
    tokens = m.get("tokens") or []
    result = []
    for t in tokens:
        tid = t.get("token_id") or t.get("tokenId") or t.get("id", "")
        outcome = t.get("outcome", "")
        if tid:
            result.append((tid, market_id, outcome))
    return result


async def fetch_markets_by_ids(market_ids: list[str]) -> list[dict]:
    """Fetch specific markets by ID from the Gamma API."""
    if not market_ids:
        return []
    c = _client()
    results = []
    for mid in market_ids:
        try:
            r = await c.get(f"{GAMMA}/markets/{mid}")
            r.raise_for_status()
            results.append(r.json())
            await asyncio.sleep(0.1)
        except Exception as exc:
            log.warning("gamma_supplemental_fetch_error", market_id=mid, error=str(exc))
    log.info("gamma_supplemental_fetched", requested=len(market_ids), got=len(results))
    return results


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def fetch_orderbook(token_id: str) -> dict | None:
    """Fetch a single order book from the CLOB REST API."""
    c = _client()
    try:
        r = await c.get(f"{CLOB}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("clob_book_fetch_error", token_id=token_id, error=str(exc))
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def fetch_midpoints_batch(token_ids: list[str]) -> dict[str, float]:
    """Fetch midpoints for many tokens in one request."""
    if not token_ids:
        return {}
    c = _client()
    try:
        r = await c.get(f"{CLOB}/midpoints", params={"token_ids": ",".join(token_ids)})
        r.raise_for_status()
        data = r.json()
        # Response: {"mid": {"token_id": price, ...}} or list
        mids = data.get("mid") or {}
        return {k: float(v) for k, v in mids.items()}
    except Exception as exc:
        log.warning("clob_midpoints_error", error=str(exc))
        return {}


def book_to_price_record(book: dict, token_id: str, ts: datetime | None = None) -> dict:
    """Convert a raw CLOB book response to a price dict ready for DB insert."""
    asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 0)))
    bids = sorted(book.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None

    return {
        "ts": ts or datetime.now(timezone.utc),
        "token_id": token_id,
        "source": "polymarket",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "bid_size_top": float(bids[0]["size"]) if bids else None,
        "ask_size_top": float(asks[0]["size"]) if asks else None,
    }


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
