"""Elo / Glicko-2 sports rating models for win-probability estimation.

Maintains a per-league rating store and converts Elo diff → win probability.
Compares resulting probability against Polymarket ask to detect soft edges.

Usage:
    store = EloStore.load_or_create("nfl")
    store.update("Chiefs", "Eagles", home_won=True)
    p = store.win_prob("Chiefs", "Eagles", home_advantage=True)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Logistic Elo: p = 1 / (1 + 10^(−Δ/N))
# N=400 → standard chess Elo; N=500 → wider, used by 538 for NFL/NBA
ELO_N = {
    "nfl": 500,
    "nba": 500,
    "nhl": 500,
    "soccer": 600,
    "tennis": 400,
    "default": 400,
}

# K-factor (how much each game moves ratings)
ELO_K = {
    "nfl": 20,
    "nba": 20,
    "nhl": 20,
    "soccer": 20,
    "tennis": 32,
    "default": 20,
}

# Home advantage in Elo points
HOME_ADVANTAGE = {
    "nfl": 55,
    "nba": 100,
    "nhl": 60,
    "soccer": 100,
    "tennis": 0,
    "default": 65,
}

# Mean-reversion between seasons (regress this fraction toward 1500)
SEASON_REVERSION = 0.33


@dataclass
class EloStore:
    league: str
    ratings: dict[str, float] = field(default_factory=dict)
    n_games: dict[str, int] = field(default_factory=dict)

    def _n(self) -> float:
        return ELO_N.get(self.league, ELO_N["default"])

    def _k(self) -> float:
        return ELO_K.get(self.league, ELO_K["default"])

    def _home_adv(self) -> float:
        return HOME_ADVANTAGE.get(self.league, HOME_ADVANTAGE["default"])

    def get_rating(self, team: str) -> float:
        return self.ratings.get(team, 1500.0)

    def expected_score(self, team_a: str, team_b: str, home_team: str | None = None) -> float:
        """P(team_a wins) with optional home-field adjustment."""
        ra = self.get_rating(team_a)
        rb = self.get_rating(team_b)
        if home_team == team_a:
            ra += self._home_adv()
        elif home_team == team_b:
            rb += self._home_adv()
        return 1.0 / (1.0 + 10.0 ** ((rb - ra) / self._n()))

    def win_prob(self, team_a: str, team_b: str, home_team: str | None = None) -> float:
        """Alias for expected_score — P(team_a wins)."""
        return self.expected_score(team_a, team_b, home_team)

    def update(
        self,
        home_team: str,
        away_team: str,
        home_score: float,
        away_score: float,
    ) -> None:
        """Update ratings after a game. Scores can be goals (soccer) or just 1/0/0.5."""
        actual = 1.0 if home_score > away_score else (0.5 if home_score == away_score else 0.0)
        expected = self.expected_score(home_team, away_team, home_team=home_team)
        k = self._k()

        rh = self.get_rating(home_team)
        ra = self.get_rating(away_team)
        self.ratings[home_team] = rh + k * (actual - expected)
        self.ratings[away_team] = ra + k * ((1.0 - actual) - (1.0 - expected))
        self.n_games[home_team] = self.n_games.get(home_team, 0) + 1
        self.n_games[away_team] = self.n_games.get(away_team, 0) + 1

    def season_reset(self, reversion: float = SEASON_REVERSION) -> None:
        """Regress all ratings toward 1500 (called at end of season)."""
        for team in list(self.ratings):
            self.ratings[team] = self.ratings[team] * (1.0 - reversion) + 1500.0 * reversion

    def to_dict(self) -> dict:
        return {"league": self.league, "ratings": self.ratings, "n_games": self.n_games}

    @classmethod
    def from_dict(cls, d: dict) -> "EloStore":
        s = cls(league=d["league"])
        s.ratings = d.get("ratings", {})
        s.n_games = d.get("n_games", {})
        return s

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load_or_create(cls, league: str, path: Path | None = None) -> "EloStore":
        if path is None:
            path = Path(f"/tmp/elo_{league}.json")
        if path.exists():
            return cls.from_dict(json.loads(path.read_text()))
        log.info("elo_store_created", league=league)
        return cls(league=league)


# ── Glicko-2 (simplified scalar version for quick use) ────────────────────

@dataclass
class Glicko2Player:
    """Simplified Glicko-2 scalar tracker (single player vs field)."""
    rating: float = 1500.0
    rd: float = 350.0      # Rating Deviation
    vol: float = 0.06      # Volatility

    TAU = 0.5              # system constant (constrains vol change)

    def win_prob_vs(self, opp_rating: float, opp_rd: float = 100.0) -> float:
        q = math.log(10) / 400.0
        g_rd = 1.0 / math.sqrt(1.0 + 3.0 * q**2 * opp_rd**2 / math.pi**2)
        return 1.0 / (1.0 + 10.0 ** (-g_rd * (self.rating - opp_rating) / 400.0))
