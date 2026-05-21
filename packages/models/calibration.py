"""Calibration tracking and Brier score computation.

Every hour we snapshot (market_id, model_p, market_p) for all watched markets.
When a market resolves, we label it 0/1 and compute Brier score / log-loss.

Used by /calibration bot command and by the soft-edge detector to demote
models that are consistently mis-calibrated.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger()


def brier_score(model_p: float, outcome: int) -> float:
    """Lower is better. Perfect forecast = 0; uninformative = 0.25."""
    return (model_p - outcome) ** 2


def log_loss(model_p: float, outcome: int) -> float:
    p = max(1e-6, min(1 - 1e-6, model_p))
    return -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))


@dataclass
class CalibrationBucket:
    """Reliability diagram bucket."""
    low: float
    high: float
    predictions: list[float] = field(default_factory=list)
    outcomes: list[int] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def mean_predicted(self) -> float | None:
        return sum(self.predictions) / len(self.predictions) if self.predictions else None

    @property
    def mean_observed(self) -> float | None:
        return sum(self.outcomes) / len(self.outcomes) if self.outcomes else None

    def calibration_error(self) -> float | None:
        mp, mo = self.mean_predicted, self.mean_observed
        if mp is None or mo is None:
            return None
        return abs(mp - mo)


class CalibrationTracker:
    """
    Aggregates predictions vs outcomes for a given model_kind and computes
    summary calibration statistics.
    """

    def __init__(self, model_kind: str, n_buckets: int = 10) -> None:
        self.model_kind = model_kind
        self.n_buckets = n_buckets
        self.predictions: list[float] = []
        self.outcomes: list[int] = []

    def add(self, model_p: float, outcome: int) -> None:
        self.predictions.append(model_p)
        self.outcomes.append(outcome)

    def brier(self) -> float | None:
        if not self.outcomes:
            return None
        return sum(brier_score(p, o) for p, o in zip(self.predictions, self.outcomes)) / len(self.outcomes)

    def log_loss_mean(self) -> float | None:
        if not self.outcomes:
            return None
        return sum(log_loss(p, o) for p, o in zip(self.predictions, self.outcomes)) / len(self.outcomes)

    def reliability_diagram(self) -> list[dict]:
        """Group predictions into decile buckets and compute mean predicted vs observed."""
        step = 1.0 / self.n_buckets
        buckets = [
            CalibrationBucket(low=i * step, high=(i + 1) * step)
            for i in range(self.n_buckets)
        ]
        for p, o in zip(self.predictions, self.outcomes):
            idx = min(int(p / step), self.n_buckets - 1)
            buckets[idx].predictions.append(p)
            buckets[idx].outcomes.append(o)
        return [
            {
                "bucket": f"{b.low:.1f}–{b.high:.1f}",
                "n": b.n,
                "mean_predicted": round(b.mean_predicted, 3) if b.mean_predicted else None,
                "mean_observed": round(b.mean_observed, 3) if b.mean_observed else None,
                "calibration_error": round(b.calibration_error(), 3) if b.calibration_error() else None,
            }
            for b in buckets
            if b.n > 0
        ]

    def summary(self) -> dict:
        return {
            "model_kind": self.model_kind,
            "n": len(self.outcomes),
            "brier": round(self.brier(), 4) if self.brier() else None,
            "log_loss": round(self.log_loss_mean(), 4) if self.log_loss_mean() else None,
            "reliability": self.reliability_diagram(),
        }


# ── DB helpers ────────────────────────────────────────────────────────────

async def snapshot_calibration(
    pool: asyncpg.Pool,
    market_id: str,
    model_p: float,
    market_p: float,
    model_kind: str,
) -> None:
    """Insert a calibration snapshot row (outcome=NULL until market resolves)."""
    await pool.execute(
        """
        INSERT INTO calibration_snapshots (market_id, model_p, market_p, model_kind)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        market_id, model_p, market_p, model_kind,
    )


async def mark_resolved(
    pool: asyncpg.Pool,
    market_id: str,
    outcome: int,          # 0 or 1
) -> None:
    """Label all unresolved snapshots for a market with the final outcome."""
    n = await pool.execute(
        "UPDATE calibration_snapshots SET outcome=$1 WHERE market_id=$2 AND outcome IS NULL",
        outcome, market_id,
    )
    log.info("calibration_marked_resolved", market_id=market_id, outcome=outcome, updated=n)


async def load_tracker(
    pool: asyncpg.Pool,
    model_kind: str,
    days: int = 30,
) -> CalibrationTracker:
    """Load a CalibrationTracker from DB rows for the last N days."""
    rows = await pool.fetch(
        """
        SELECT model_p, outcome FROM calibration_snapshots
        WHERE model_kind=$1
          AND outcome IS NOT NULL
          AND ts > now() - ($2 || ' days')::interval
        """,
        model_kind, str(days),
    )
    tracker = CalibrationTracker(model_kind)
    for r in rows:
        tracker.add(r["model_p"], r["outcome"])
    return tracker
