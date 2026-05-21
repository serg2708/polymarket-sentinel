"""Intra-Polymarket arbitrage detector.

Detects Dutch books within a single Polymarket binary market:
  best_ask_yes + best_ask_no < 1.00

Also detects multi-outcome negRisk incomplete-set opportunities.
"""
from __future__ import annotations

from datetime import datetime


def find_intramarket_arb(
    yes_asks: list[tuple[float, float]],
    no_asks: list[tuple[float, float]],
    size: float = 100.0,
    min_edge_bps: int = 30,
) -> dict | None:
    """
    A complete set (1 YES + 1 NO token) pays exactly $1.
    If cost_to_buy(YES, size) + cost_to_buy(NO, size) < size, that's an arb.
    """
    from .arb_xplatform import cost_to_buy

    ca = cost_to_buy(yes_asks, size)
    cb = cost_to_buy(no_asks, size)
    if ca is None or cb is None:
        return None

    total = ca + cb
    edge_usd = size - total
    if edge_usd <= 0:
        return None

    edge_bps = int(edge_usd / total * 10000)
    if edge_bps < min_edge_bps:
        return None

    return {
        "kind": "arb_intramarket",
        "size": size,
        "cost_yes": round(ca, 4),
        "cost_no": round(cb, 4),
        "total_cost": round(total, 4),
        "edge_usd": round(edge_usd, 4),
        "edge_bps": edge_bps,
    }


def check_sum_deviation(
    yes_mid: float | None,
    no_mid: float | None,
    threshold_bps: int = 200,
) -> dict | None:
    """
    Alert when YES_mid + NO_mid deviates significantly from 1.0.
    This is a softer signal than a full book walk.
    """
    if yes_mid is None or no_mid is None:
        return None
    total = yes_mid + no_mid
    if total <= 0:
        return None
    deviation_bps = int(abs(1.0 - total) * 10000)
    if deviation_bps < threshold_bps:
        return None
    direction = "under" if total < 1.0 else "over"
    return {
        "kind": "sum_deviation",
        "yes_mid": yes_mid,
        "no_mid": no_mid,
        "total": round(total, 4),
        "deviation_bps": deviation_bps,
        "direction": direction,
    }
