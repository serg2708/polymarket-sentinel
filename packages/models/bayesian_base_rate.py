"""Bayesian base-rate priors for common prediction market categories.

These are structural priors to combine with polling/market data.
Think of them as the "fundamentals model" before any market-specific information.

References:
- Incumbency advantage: ~+5pp historically (US presidential, varies by cycle)
- Economic model (Bread and Peace, Abramowitz): GDP growth + presidential approval
- Sports base rates: ~50% for roughly equal matchups, skewed by Elo
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


# ── Generic base-rate registry ────────────────────────────────────────────

_BASE_RATES: dict[str, float] = {
    # US political
    "us_incumbent_wins_reelection": 0.68,          # post-WW2 historical
    "incumbent_party_wins_open_seat": 0.55,
    "challenger_wins_vs_incumbent": 0.32,

    # General elections
    "polling_leader_wins": 0.85,                   # rough rule of thumb
    "polling_leader_wins_within_3pp": 0.65,

    # Crypto / macro
    "crypto_up_any_given_month": 0.55,             # very rough
    "fed_hikes_when_inflation_above_3pct": 0.75,
    "fed_cuts_when_unemployment_above_5pct": 0.70,

    # Sports
    "home_team_wins_nfl": 0.57,
    "home_team_wins_nba": 0.60,
    "home_team_wins_soccer": 0.46,
    "home_team_wins_nhl": 0.55,
}


def get_base_rate(key: str) -> float | None:
    return _BASE_RATES.get(key)


def register_base_rate(key: str, p: float) -> None:
    _BASE_RATES[key] = max(0.01, min(0.99, p))


# ── Log-odds blending ─────────────────────────────────────────────────────

def logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1.0 - p))


def inv_logit(l: float) -> float:
    return 1.0 / (1.0 + math.exp(-l))


def blend_logodds(
    priors: list[tuple[float, float]],  # (probability, weight)
) -> float:
    """
    Blend multiple probability estimates in log-odds space.
    Each weight should sum to 1.0 (normalized internally if not).
    """
    total_w = sum(w for _, w in priors)
    if total_w == 0:
        return 0.5
    logodds = sum(logit(p) * (w / total_w) for p, w in priors)
    return inv_logit(logodds)


# ── Economic fundamentals model (simplified Bread & Peace) ───────────────

@dataclass
class EconomicFundamentals:
    """
    Very simplified structural model for US presidential elections.
    Uses GDP growth (Q2 election year) and presidential approval.
    """
    gdp_growth_q2: float | None = None     # annualised % (e.g. 2.5)
    approval_rating: float | None = None   # net approval (e.g. -5 for 47% approve, 52% disapprove)
    incumbent_party: bool = True           # True = incumbent party's candidate

    # Abramowitz-style coefficients (rough approximation)
    _INTERCEPT = 0.52
    _COEF_GDP = 0.008       # per % point of Q2 GDP growth
    _COEF_APPROVAL = 0.003  # per net approval point
    _INCUMBENT_BOOST = 0.05

    def predict(self) -> float:
        p = self._INTERCEPT
        if self.gdp_growth_q2 is not None:
            p += self._COEF_GDP * self.gdp_growth_q2
        if self.approval_rating is not None:
            p += self._COEF_APPROVAL * self.approval_rating
        if self.incumbent_party:
            p += self._INCUMBENT_BOOST
        return max(0.05, min(0.95, p))


# ── Bayesian update (Beta-Binomial) ──────────────────────────────────────

@dataclass
class BetaPrior:
    """
    Simple Beta-Binomial Bayesian update.
    Start with a prior (alpha, beta), update with observed outcomes.
    """
    alpha: float = 1.0   # pseudo-successes
    beta: float = 1.0    # pseudo-failures

    @classmethod
    def from_prior_p(cls, p: float, strength: float = 10.0) -> "BetaPrior":
        """
        Create a Beta prior from a probability and 'effective sample size'.
        strength=10 means the prior has weight of 10 observations.
        """
        return cls(alpha=p * strength, beta=(1.0 - p) * strength)

    def update(self, successes: int, failures: int) -> "BetaPrior":
        return BetaPrior(
            alpha=self.alpha + successes,
            beta=self.beta + failures,
        )

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def mode(self) -> float:
        if self.alpha > 1 and self.beta > 1:
            return (self.alpha - 1) / (self.alpha + self.beta - 2)
        return self.mean

    @property
    def variance(self) -> float:
        n = self.alpha + self.beta
        return self.alpha * self.beta / (n**2 * (n + 1))

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    def credible_interval(self, level: float = 0.95) -> tuple[float, float]:
        from scipy.stats import beta as beta_dist
        lo = (1.0 - level) / 2
        hi = 1.0 - lo
        return beta_dist.ppf(lo, self.alpha, self.beta), beta_dist.ppf(hi, self.alpha, self.beta)

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
        }
