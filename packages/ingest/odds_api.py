"""Sportsbook odds ingest via OddsPapi (free tier includes Pinnacle).

OddsPapi: https://oddspapi.io
Free tier: 250 req/month (use sparingly).

For each sports event matched to a Polymarket market, fetches Pinnacle odds
and converts to implied probability for the soft-edge detector.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from ..common.settings import get_settings

log = structlog.get_logger()

ODDSPAPI_BASE = "https://api.oddspapi.io/v1"


class OddsAPIClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=ODDSPAPI_BASE,
            timeout=20,
            headers={"x-api-key": get_settings().oddspapi_api_key},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def get_sports(self) -> list[dict]:
        r = await self._http.get("/sports")
        r.raise_for_status()
        return r.json().get("data", [])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def get_odds(
        self,
        sport_key: str,
        bookmakers: str = "pinnacle",
        markets: str = "h2h",
        regions: str = "eu",
    ) -> list[dict]:
        """Fetch moneyline (h2h) odds for a sport from Pinnacle."""
        r = await self._http.get(
            f"/odds/{sport_key}",
            params={
                "bookmakers": bookmakers,
                "markets": markets,
                "regions": regions,
            },
        )
        r.raise_for_status()
        return r.json().get("data", [])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    async def get_event_odds(self, event_id: str, bookmakers: str = "pinnacle") -> dict | None:
        try:
            r = await self._http.get(f"/odds/event/{event_id}", params={"bookmakers": bookmakers})
            r.raise_for_status()
            return r.json().get("data")
        except Exception as exc:
            log.warning("oddspapi_event_error", event_id=event_id, error=str(exc))
            return None


def american_to_prob(odds: float) -> float:
    """Convert American/moneyline odds to implied probability (including vig)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


def decimal_to_prob(odds: float) -> float:
    """Convert decimal odds to implied probability (including vig)."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def remove_vig(p_home: float, p_away: float) -> tuple[float, float]:
    """Remove the bookmaker vig (overround) from a two-way market."""
    total = p_home + p_away
    if total <= 0:
        return 0.5, 0.5
    return p_home / total, p_away / total


def parse_h2h_event(event: dict) -> dict | None:
    """
    Extract Pinnacle no-vig win probabilities from an OddsPapi event record.

    Returns {home_team, away_team, home_p, away_p, commence_time, event_id}
    or None if no Pinnacle data found.
    """
    pinnacle = None
    for book in event.get("bookmakers", []):
        if "pinnacle" in book.get("key", "").lower():
            pinnacle = book
            break
    if not pinnacle:
        return None

    h2h = None
    for market in pinnacle.get("markets", []):
        if market.get("key") == "h2h":
            h2h = market
            break
    if not h2h:
        return None

    outcomes = h2h.get("outcomes", [])
    if len(outcomes) < 2:
        return None

    # Prefer decimal odds; fall back to american
    probs = []
    for o in outcomes:
        price = o.get("price") or o.get("decimal_odds") or o.get("american_odds")
        if price is None:
            return None
        if isinstance(price, str):
            price = float(price)
        # Heuristic: decimal odds > 1.05 look like decimal; otherwise American
        if price > 1.05:
            probs.append(decimal_to_prob(price))
        else:
            probs.append(american_to_prob(price))

    home_p_raw, away_p_raw = probs[0], probs[1]
    home_p, away_p = remove_vig(home_p_raw, away_p_raw)

    return {
        "event_id": event.get("id"),
        "sport_key": event.get("sport_key"),
        "home_team": outcomes[0].get("name"),
        "away_team": outcomes[1].get("name"),
        "home_p": round(home_p, 4),
        "away_p": round(away_p, 4),
        "commence_time": event.get("commence_time"),
        "source": "pinnacle_via_oddspapi",
    }


# ── The Odds API (soft-book fallback, 500 credits/month free) ─────────────

THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


async def fetch_the_odds_api(sport: str, api_key: str | None = None) -> list[dict]:
    """
    Fallback to The Odds API (no Pinnacle on free tier but easier setup).
    Returns list of parsed events with no-vig h2h probabilities.
    """
    key = api_key or get_settings().oddspapi_api_key  # try reuse; they're different APIs
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{THE_ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey": key,
                    "regions": "eu,uk",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            r.raise_for_status()
            events = r.json()
            log.debug(
                "the_odds_api_remaining",
                remaining=r.headers.get("x-requests-remaining"),
            )
    except Exception as exc:
        log.warning("the_odds_api_error", sport=sport, error=str(exc))
        return []

    parsed = []
    for ev in events:
        p = parse_h2h_event(ev)
        if p:
            parsed.append(p)
    return parsed
