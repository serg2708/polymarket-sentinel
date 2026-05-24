"""Liquidity and orderbook anomaly detector.

Detects signals that often precede or reveal mispricings:
- Wide spread relative to midpoint
- Z-score price spike
- Volume spike
- Stale book
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import NamedTuple

import math


class PricePoint(NamedTuple):
    ts: datetime
    mid: float
    volume: float = 0.0


class TokenStats:
    """Rolling statistics for a single token."""

    def __init__(self, window: int = 60) -> None:
        self._window = window
        self._prices: deque[PricePoint] = deque(maxlen=window)

    def update(self, mid: float, volume: float = 0.0) -> None:
        self._prices.append(PricePoint(ts=datetime.now(timezone.utc), mid=mid, volume=volume))

    @property
    def n(self) -> int:
        return len(self._prices)

    @property
    def mean(self) -> float | None:
        if not self._prices:
            return None
        return sum(p.mid for p in self._prices) / len(self._prices)

    @property
    def stddev(self) -> float | None:
        if len(self._prices) < 2:
            return None
        m = self.mean
        variance = sum((p.mid - m) ** 2 for p in self._prices) / (len(self._prices) - 1)
        return math.sqrt(variance)

    def z_score(self, current_mid: float) -> float | None:
        m, s = self.mean, self.stddev
        if m is None or s is None or s == 0:
            return None
        return (current_mid - m) / s

    @property
    def recent_volume(self) -> float:
        return sum(p.volume for p in self._prices)


# Global per-token stats store
_token_stats: dict[str, TokenStats] = {}


def get_stats(token_id: str) -> TokenStats:
    if token_id not in _token_stats:
        _token_stats[token_id] = TokenStats(window=60)
    return _token_stats[token_id]


def check_spread(
    best_bid: float | None,
    best_ask: float | None,
    spread_threshold: float = 0.05,
) -> dict | None:
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    spread = best_ask - best_bid
    rel_spread = spread / mid
    if rel_spread > spread_threshold:
        return {
            "kind": "wide_spread",
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "spread": round(spread, 4),
            "rel_spread": round(rel_spread, 4),
            "threshold": spread_threshold,
        }
    return None


def check_z_score(
    token_id: str,
    current_mid: float,
    z_threshold: float = 3.0,
    min_abs_move: float = 0.01,
) -> dict | None:
    stats = get_stats(token_id)
    stats.update(current_mid)
    z = stats.z_score(current_mid)
    if z is not None and abs(z) >= z_threshold:
        if stats.mean is not None and abs(current_mid - stats.mean) < min_abs_move:
            return None  # statistically significant but too small to matter (<1¢)
        return {
            "kind": "price_spike",
            "current_mid": round(current_mid, 4),
            "rolling_mean": round(stats.mean, 4),
            "rolling_std": round(stats.stddev, 6),
            "z_score": round(z, 2),
            "n_samples": stats.n,
        }
    return None


def check_all(
    token_id: str,
    best_bid: float | None,
    best_ask: float | None,
    bid_size: float | None,
    ask_size: float | None,
    spread_threshold: float = 0.05,
    imbalance_ratio: float = 5.0,
    z_threshold: float = 3.0,
) -> list[dict]:
    """Run all anomaly checks; return list of hit dicts."""
    hits = []
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None

    s = check_spread(best_bid, best_ask, spread_threshold)
    if s:
        hits.append(s)

    if mid is not None:
        z = check_z_score(token_id, mid, z_threshold)
        if z:
            hits.append(z)

    return hits
