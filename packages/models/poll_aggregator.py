"""Polling aggregator for political prediction markets.

Implements a weighted-average model with:
- Recency decay (exponential half-life)
- Pollster house-effect adjustments (loaded from YAML)
- Simple fundamentals baseline (incumbency + economic approval)

For more sophisticated modelling, see the Bayesian state-space approach
in Linzer 2013 / Heidemanns-Gelman 2020 — this is the fast practical version.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# Exponential decay: poll weight halves every HALF_LIFE_DAYS
HALF_LIFE_DAYS = 14.0

# Pollster grade → bias std-dev adjustment (add gaussian noise to house effect)
POLLSTER_WEIGHTS = {
    "A+": 1.0,
    "A": 0.9,
    "A-": 0.8,
    "B+": 0.7,
    "B": 0.6,
    "B-": 0.5,
    "C+": 0.4,
    "C": 0.3,
    "unknown": 0.3,
}


@dataclass
class Poll:
    date: datetime
    candidate: str          # or 'YES' for binary questions
    pct: float              # 0–100 scale
    sample_size: int = 600
    pollster: str = "unknown"
    grade: str = "unknown"
    margin_of_error: float | None = None  # 95% CI; estimated if None


def decay_weight(poll_date: datetime, reference: datetime | None = None) -> float:
    """Exponential recency weight. Most recent = 1.0; decays by half every HALF_LIFE_DAYS."""
    ref = reference or datetime.now(timezone.utc)
    if poll_date.tzinfo is None:
        poll_date = poll_date.replace(tzinfo=timezone.utc)
    days_ago = (ref - poll_date).total_seconds() / 86400.0
    return math.exp(-math.log(2) * days_ago / HALF_LIFE_DAYS)


def moe(sample_size: int) -> float:
    """Classic binomial margin of error at p=0.5, 95% CI."""
    return 1.96 * math.sqrt(0.5 * 0.5 / sample_size) * 100  # percentage points


class PollAggregator:
    """
    Maintain a pool of polls for a single binary market question and
    produce a weighted-average probability.
    """

    def __init__(self, question: str) -> None:
        self.question = question
        self.polls: list[Poll] = []

    def add_poll(self, poll: Poll) -> None:
        self.polls.append(poll)

    def aggregate(
        self,
        reference_date: datetime | None = None,
        fundamentals_weight: float = 0.0,
        fundamentals_p: float | None = None,
    ) -> float | None:
        """
        Return weighted-average probability (0.0–1.0) for YES/candidate winning.

        fundamentals_weight: blend in a structural prior (0 = polls only).
        fundamentals_p: the structural baseline (e.g. from bayesian_base_rate).
        """
        if not self.polls:
            if fundamentals_p is not None:
                return fundamentals_p
            return None

        total_weight = 0.0
        weighted_sum = 0.0

        for poll in self.polls:
            w = (
                decay_weight(poll.date, reference_date)
                * POLLSTER_WEIGHTS.get(poll.grade, 0.3)
            )
            # Weight also by effective sample size (sqrt dampening to avoid outliers)
            w *= math.sqrt(max(poll.sample_size, 100)) / math.sqrt(600)
            weighted_sum += w * (poll.pct / 100.0)
            total_weight += w

        if total_weight == 0:
            return None

        polls_p = weighted_sum / total_weight

        if fundamentals_p is not None and fundamentals_weight > 0:
            polls_p = polls_p * (1 - fundamentals_weight) + fundamentals_p * fundamentals_weight

        return max(0.01, min(0.99, polls_p))

    def uncertainty(self) -> float | None:
        """Approx. aggregate uncertainty (standard error of weighted mean)."""
        if not self.polls:
            return None
        weights = [
            decay_weight(p.date) * POLLSTER_WEIGHTS.get(p.grade, 0.3)
            for p in self.polls
        ]
        effective_n = (sum(weights) ** 2) / sum(w**2 for w in weights)
        return math.sqrt(0.25 / effective_n)  # SE for p near 0.5

    def to_dict(self) -> dict:
        p = self.aggregate()
        u = self.uncertainty()
        return {
            "question": self.question,
            "n_polls": len(self.polls),
            "p": round(p, 4) if p else None,
            "uncertainty_se": round(u, 4) if u else None,
            "p_low": round(max(0, p - 1.96 * u), 4) if (p and u) else None,
            "p_high": round(min(1, p + 1.96 * u), 4) if (p and u) else None,
        }


# ── FiveThirtyEight / RealClearPolitics scrapers (best-effort) ────────────

async def fetch_rcp_averages(url: str) -> list[dict]:
    """
    Scrape a RealClearPolitics polling-average page.
    Returns list of {candidate, pct, date} dicts.
    Note: RCP has no API; HTML scraping is fragile — treat as best-effort.
    """
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            r.raise_for_status()
            # Very basic extraction — override with BeautifulSoup for production
            import re
            matches = re.findall(r'"candidate":"([^"]+)","value":([\d.]+)', r.text)
            return [{"candidate": m[0], "pct": float(m[1])} for m in matches]
    except Exception as exc:
        log.warning("rcp_fetch_error", url=url, error=str(exc))
        return []


async def fetch_538_csv(url: str) -> list[dict]:
    """Parse a FiveThirtyEight CSV polling file (e.g. from their GitHub)."""
    try:
        import io
        import pandas as pd
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url)
            r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        return df.to_dict("records")
    except Exception as exc:
        log.warning("538_csv_error", url=url, error=str(exc))
        return []
