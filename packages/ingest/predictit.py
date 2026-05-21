"""PredictIt market data client.

Real-money US prediction market. API is geo-restricted to US IPs.
Market data format: market_id/contract_id (e.g. "7456/21897")
For binary single-contract markets, market_id alone is sufficient.

Fee: PredictIt charges 10% on profits and 5% on withdrawals.
Kelly fractions are reduced by 10% to account for profit fee.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger()

PREDICTIT_BASE = "https://www.predictit.org/api/marketdata"

# PredictIt profit fee: 10% of winnings. Reduces effective payout from $1 to $0.90.
PROFIT_FEE = 0.10


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=15))
async def get_contract(market_contract_id: str) -> dict | None:
    """Fetch a single PredictIt contract by 'market_id/contract_id' or 'market_id'.

    Returns a dict with:
      - implied_prob: best YES ask price (what you pay to buy YES)
      - yes_ask: best buy YES cost
      - no_ask: best buy NO cost
      - yes_bid: best sell YES cost
      - volume: volume_24h (not available in API, set to None)
    """
    parts = str(market_contract_id).split("/")
    market_id = parts[0]
    contract_id = parts[1] if len(parts) > 1 else None

    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(f"{PREDICTIT_BASE}/markets/{market_id}/")
            r.raise_for_status()
            data = r.json()
            contracts = data.get("contracts") or []

            if contract_id:
                contract = next(
                    (ct for ct in contracts if str(ct.get("id")) == str(contract_id)),
                    None,
                )
            elif len(contracts) == 1:
                contract = contracts[0]
            else:
                # Multi-contract market without specified contract — skip
                log.warning(
                    "predictit_multi_contract_no_id",
                    market_id=market_id,
                    n_contracts=len(contracts),
                )
                return None

            if not contract:
                log.warning("predictit_contract_not_found", market_contract_id=market_contract_id)
                return None

            yes_ask = contract.get("bestBuyYesCost")
            no_ask = contract.get("bestBuyNoCost")
            yes_bid = contract.get("bestSellYesCost")

            if yes_ask is None:
                return None

            return {
                "market_id": market_id,
                "contract_id": contract.get("id"),
                "name": contract.get("shortName") or data.get("shortName"),
                "source": "predictit",
                "implied_prob": float(yes_ask),
                "yes_ask": float(yes_ask),
                "no_ask": float(no_ask) if no_ask is not None else None,
                "yes_bid": float(yes_bid) if yes_bid is not None else None,
                "last_trade": contract.get("lastTradePrice"),
                "status": contract.get("status"),
            }
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                log.warning(
                    "predictit_geo_blocked",
                    market_id=market_id,
                    note="PredictIt API requires US IP — deploy on US server",
                )
                return None
            log.warning("predictit_http_error", market_id=market_id, status=exc.response.status_code)
            return None
        except Exception as exc:
            log.warning("predictit_error", market_contract_id=market_contract_id, error=str(exc))
            return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=10))
async def get_all_markets() -> list[dict]:
    """Fetch all PredictIt markets. Used for auto-discovery."""
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.get(f"{PREDICTIT_BASE}/all/")
            r.raise_for_status()
            return r.json().get("markets", [])
        except Exception as exc:
            log.warning("predictit_all_markets_error", error=str(exc))
            return []
