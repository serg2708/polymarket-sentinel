"""Sports model integration: Elo win-prob → soft-edge alert.

Fetches historical game results from nflverse/sports data sources,
maintains per-league Elo stores, and computes win probabilities
for upcoming games matched to Polymarket.

Usage from a script or notebook:
    from packages.models.sports_model import SportsEdgeEngine

    engine = SportsEdgeEngine("nfl")
    await engine.load_history_from_csv("/path/to/nfl_games.csv")
    p = engine.win_prob("Kansas City Chiefs", "Philadelphia Eagles", home="Kansas City Chiefs")
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import httpx
import structlog

from .elo_sports import EloStore

log = structlog.get_logger()

# Public game-result datasets (CSV, freely available)
DATA_SOURCES = {
    "nfl": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "tennis_atp": "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2024.csv",
}


class SportsEdgeEngine:
    """
    Wraps EloStore with automatic history loading and match probability lookup.
    Designed to run in the background and be queried by the soft-edge detector.
    """

    def __init__(self, league: str, store_path: Path | None = None) -> None:
        self.league = league
        self.store_path = store_path or Path(f"/tmp/elo_{league}.json")
        self.elo = EloStore.load_or_create(league, self.store_path)

    def win_prob(
        self,
        team_a: str,
        team_b: str,
        home: str | None = None,
    ) -> float:
        """P(team_a wins). home = which team has home advantage (optional)."""
        return self.elo.win_prob(team_a, team_b, home_team=home)

    def ev_signal(
        self,
        team_a: str,
        team_b: str,
        poly_ask: float,
        home: str | None = None,
    ) -> dict | None:
        """
        Return EV signal if model probability differs meaningfully from Polymarket.
        Returns None if edge < 200 bps.
        """
        model_p = self.win_prob(team_a, team_b, home)
        if poly_ask <= 0 or poly_ask >= 1:
            return None
        edge_pp = model_p - poly_ask
        edge_bps = int(abs(edge_pp) * 10000)
        if edge_bps < 200:
            return None
        return {
            "model_p": round(model_p, 4),
            "poly_ask": round(poly_ask, 4),
            "edge_pp": round(edge_pp * 100, 2),
            "edge_bps": edge_bps,
            "direction": "YES_underpriced" if edge_pp > 0 else "NO_underpriced",
            "team_a": team_a,
            "team_b": team_b,
            "league": self.league,
        }

    async def load_history_from_csv(self, path: str | Path) -> int:
        """Load game results from a local CSV file. Returns number of games loaded."""
        import pandas as pd
        path = Path(path)
        if not path.exists():
            log.warning("game_history_csv_not_found", path=str(path))
            return 0

        df = pd.read_csv(path)
        n = 0
        for _, row in df.iterrows():
            try:
                home = _get(row, ["home_team", "home", "team1"])
                away = _get(row, ["away_team", "away", "team2"])
                home_score = _get_float(row, ["home_score", "score1", "pts_h"])
                away_score = _get_float(row, ["away_score", "score2", "pts_a"])
                if home and away and home_score is not None and away_score is not None:
                    self.elo.update(home, away, home_score, away_score)
                    n += 1
            except Exception:
                continue

        self.elo.save(self.store_path)
        log.info("elo_history_loaded", league=self.league, n_games=n)
        return n

    async def download_and_load(self) -> int:
        """Download the default dataset for this league and load it."""
        url = DATA_SOURCES.get(self.league)
        if not url:
            log.warning("no_default_datasource", league=self.league)
            return 0
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(url)
                r.raise_for_status()
                csv_text = r.text
        except Exception as exc:
            log.warning("download_failed", league=self.league, error=str(exc))
            return 0

        import pandas as pd
        df = pd.read_csv(io.StringIO(csv_text))
        local = Path(f"/tmp/{self.league}_games.csv")
        local.write_text(csv_text)
        return await self.load_history_from_csv(local)


def _get(row: Any, keys: list[str]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _get_float(row: Any, keys: list[str]) -> float | None:
    for k in keys:
        v = row.get(k)
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None
