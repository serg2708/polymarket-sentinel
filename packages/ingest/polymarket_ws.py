"""Polymarket WebSocket market channel subscriber."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, Awaitable

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

log = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_MAX_DELAY = 60
RECONNECT_RESET_HOURS = 24


async def stream_market(
    token_ids: list[str],
    on_event: Callable[[dict], Awaitable[None]],
) -> None:
    """
    Subscribe to the Polymarket market WS channel for the given token IDs.
    Reconnects with exponential backoff on any error.
    Resets the subscription every RECONNECT_RESET_HOURS to prevent drift.
    """
    backoff = 1
    connected_at: float | None = None

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
            ) as ws:
                await ws.send(
                    json.dumps({
                        "assets_ids": token_ids,
                        "type": "market",
                    })
                )
                log.info("poly_ws_connected", n_tokens=len(token_ids))
                connected_at = asyncio.get_event_loop().time()
                backoff = 1

                async for raw in ws:
                    # Parse — messages can be a JSON array or single object
                    try:
                        parsed = json.loads(raw)
                        events = parsed if isinstance(parsed, list) else [parsed]
                    except json.JSONDecodeError:
                        log.warning("poly_ws_bad_json", raw=raw[:200])
                        continue

                    for ev in events:
                        try:
                            await on_event(ev)
                        except Exception as exc:
                            log.error("poly_ws_handler_error", error=str(exc))

                    # Proactive reconnect after 24 h to keep subscription fresh
                    if connected_at and (asyncio.get_event_loop().time() - connected_at) > (
                        RECONNECT_RESET_HOURS * 3600
                    ):
                        log.info("poly_ws_proactive_reconnect")
                        await ws.close()
                        break

        except ConnectionClosed as exc:
            log.warning("poly_ws_closed", code=exc.code, reason=exc.reason, backoff=backoff)
        except Exception as exc:
            log.warning("poly_ws_error", error=str(exc), backoff=backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_DELAY)


def parse_ws_book(ev: dict) -> dict | None:
    """Convert a WS 'book' or 'best_bid_ask' event into a price record dict."""
    event_type = ev.get("event_type", "")
    asset_id = ev.get("asset_id", "")
    ts = datetime.now(timezone.utc)

    if event_type in ("book", "price_change"):
        asks = ev.get("asks", [])
        bids = ev.get("bids", [])
        asks_sorted = sorted(asks, key=lambda x: float(x.get("price", 0)))
        bids_sorted = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)

        best_bid = float(bids_sorted[0]["price"]) if bids_sorted else None
        best_ask = float(asks_sorted[0]["price"]) if asks_sorted else None
        mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None

        return {
            "ts": ts,
            "token_id": asset_id,
            "source": "polymarket",
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "bid_size_top": float(bids_sorted[0]["size"]) if bids_sorted else None,
            "ask_size_top": float(asks_sorted[0]["size"]) if asks_sorted else None,
        }

    if event_type == "best_bid_ask":
        best_bid = _f(ev.get("best_bid"))
        best_ask = _f(ev.get("best_ask"))
        mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
        return {
            "ts": ts,
            "token_id": asset_id,
            "source": "polymarket",
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
        }

    if event_type == "last_trade_price":
        return {
            "ts": ts,
            "token_id": asset_id,
            "source": "polymarket",
            "last_trade": _f(ev.get("price")),
        }

    return None


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
