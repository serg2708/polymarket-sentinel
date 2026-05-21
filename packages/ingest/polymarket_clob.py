"""Polymarket read-only client — fetches positions and open orders by wallet address."""
from __future__ import annotations

import httpx
import structlog

from ..common.settings import get_settings

log = structlog.get_logger()

DATA_BASE = "https://data-api.polymarket.com"


async def get_positions() -> list[dict]:
    """Fetch open positions for the configured wallet address (public endpoint)."""
    s = get_settings()
    if not s.polymarket_address:
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(
                f"{DATA_BASE}/positions",
                params={"user": s.polymarket_address, "limit": 100, "sizeThreshold": ".01"},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as exc:
            log.warning("polymarket_positions_error", error=str(exc))
            return []


async def get_portfolio_value() -> float | None:
    """Fetch total portfolio value (positions + cash) from Polymarket."""
    s = get_settings()
    if not s.polymarket_address:
        return None
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(
                f"{DATA_BASE}/value",
                params={"user": s.polymarket_address},
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0].get("value") or 0)
            return None
        except Exception as exc:
            log.warning("polymarket_value_error", error=str(exc))
            return None


async def get_open_orders() -> list[dict]:
    """Fetch open orders (public CLOB endpoint, no auth required)."""
    s = get_settings()
    if not s.polymarket_address:
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(
                "https://clob.polymarket.com/data/orders",
                params={"maker_address": s.polymarket_address, "status": "LIVE"},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            log.warning("polymarket_orders_error", error=str(exc))
            return []
