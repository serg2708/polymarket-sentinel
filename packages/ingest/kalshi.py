"""Kalshi API client — read-only market data via RSA-PSS signed requests."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import retry, stop_after_attempt, wait_exponential

from ..common.settings import get_settings

log = structlog.get_logger()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _load_private_key():
    settings = get_settings()
    if settings.kalshi_private_key_b64:
        pem = base64.b64decode(settings.kalshi_private_key_b64)
    elif settings.kalshi_private_key_path:
        pem = Path(settings.kalshi_private_key_path).read_bytes()
    else:
        raise RuntimeError("No Kalshi private key configured (KALSHI_PRIVATE_KEY_PATH or _B64)")
    return serialization.load_pem_private_key(pem, password=None)


def _sign_request(method: str, path: str, body: str = "") -> dict[str, str]:
    """Generate Kalshi RSA-PSS auth headers."""
    settings = get_settings()
    ts_ms = str(int(time.time() * 1000))
    msg = ts_ms + method.upper() + path + body
    private_key = _load_private_key()
    signature = private_key.sign(
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": settings.kalshi_api_key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }


class KalshiClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=KALSHI_BASE,
            timeout=30,
            http2=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        headers = _sign_request("GET", "/trade-api/v2" + path)
        r = await self._http.get(path, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30))
    async def get_markets(self, status: str = "open", limit: int = 200) -> list[dict]:
        out, cursor = [], None
        while True:
            params: dict[str, Any] = {"status": status, "limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            markets = data.get("markets", [])
            out.extend(markets)
            cursor = data.get("cursor")
            if not cursor or len(out) >= limit:
                break
            await asyncio.sleep(0.1)
        log.info("kalshi_markets_fetched", count=len(out))
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def get_market(self, ticker: str) -> dict | None:
        try:
            data = await self._get(f"/markets/{ticker}")
            return data.get("market")
        except Exception as exc:
            log.warning("kalshi_get_market_error", ticker=ticker, error=str(exc))
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict | None:
        try:
            data = await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
            return data.get("orderbook")
        except Exception as exc:
            log.warning("kalshi_book_error", ticker=ticker, error=str(exc))
            return None


def parse_kalshi_market(m: dict) -> dict:
    return {
        "market_id": f"kalshi:{m['ticker']}",
        "source": "kalshi",
        "condition_id": None,
        "question": m.get("title"),
        "description": m.get("rules_primary", ""),
        "slug": m.get("ticker"),
        "tags": [m.get("category", "")],
        "end_date": _parse_dt(m.get("close_time") or m.get("expiration_time")),
        "tick_size": _f(m.get("tick_size")),
        "min_order_size": _f(m.get("min_settlement")),
        "fee_schedule": {"taker_fee_bps": int((m.get("taker_fees_bps") or 0))},
        "active": m.get("status") == "open",
        "raw": m,
    }


def kalshi_book_to_asks(book: dict | None) -> list[tuple[float, float]]:
    """Return [(price, size), ...] for YES asks from a Kalshi orderbook."""
    if not book:
        return []
    asks = book.get("yes", []) or []
    # Kalshi YES asks: [{price, quantity}], price in cents (0–99)
    result = []
    for level in asks:
        px = _f(level.get("price"))
        sz = _f(level.get("quantity"))
        if px is not None and sz is not None:
            result.append((px / 100.0, sz))  # convert cents to 0-1 probability
    return sorted(result, key=lambda x: x[0])


def kalshi_book_to_no_asks(book: dict | None) -> list[tuple[float, float]]:
    """Return [(price, size), ...] for NO asks from a Kalshi orderbook."""
    if not book:
        return []
    asks = book.get("no", []) or []
    result = []
    for level in asks:
        px = _f(level.get("price"))
        sz = _f(level.get("quantity"))
        if px is not None and sz is not None:
            result.append((px / 100.0, sz))
    return sorted(result, key=lambda x: x[0])


def kalshi_price_record(ticker: str, book: dict) -> dict:
    yes_asks = kalshi_book_to_asks(book)
    yes_bids_raw = book.get("yes_bids") or book.get("yes") or []
    no_asks = kalshi_book_to_no_asks(book)

    best_yes_ask = yes_asks[0][0] if yes_asks else None
    best_no_ask = no_asks[0][0] if no_asks else None
    mid = (best_yes_ask + (1.0 - best_no_ask)) / 2 if (best_yes_ask and best_no_ask) else best_yes_ask

    return {
        "ts": datetime.now(timezone.utc),
        "token_id": f"kalshi:{ticker}:YES",
        "source": "kalshi",
        "best_ask": best_yes_ask,
        "mid": mid,
        "ask_size_top": yes_asks[0][1] if yes_asks else None,
    }


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
