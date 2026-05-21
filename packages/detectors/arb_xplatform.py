"""Cross-platform arbitrage detector.

Detects opportunities where buying YES on Polymarket + NO on Kalshi
(or vice versa) costs less than $1.00 per contract, guaranteeing profit
regardless of outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Leg:
    venue: str
    token_id: str
    side: str               # 'YES' | 'NO'
    asks: list[tuple[float, float]]  # [(price, size), ...] sorted ascending
    taker_fee_bps: int = 0


def cost_to_buy(asks: list[tuple[float, float]], target_size: float) -> float | None:
    """Walk the ask side; return total cost or None if insufficient liquidity."""
    remaining, cost = target_size, 0.0
    for px, sz in asks:
        take = min(sz, remaining)
        cost += take * px
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # book too thin
    return cost


def find_arb(
    leg_a: Leg,
    leg_b: Leg,
    sizes: tuple[float, ...] = (50, 100, 250, 500, 1000),
    min_edge_bps: int = 50,
) -> dict | None:
    """
    leg_a is the YES leg, leg_b is the NO leg on the same equivalent event.
    If YES resolves TRUE → leg_a pays $1; if NO resolves TRUE → leg_b pays $1.
    Either way one side pays out $1 per contract.

    Returns the best size/edge combo or None.
    """
    best: dict | None = None

    for sz in sizes:
        ca = cost_to_buy(leg_a.asks, sz)
        cb = cost_to_buy(leg_b.asks, sz)
        if ca is None or cb is None:
            continue

        # Apply taker fees (multiply cost by 1 + fee_bps/10000)
        ca_with_fee = ca * (1 + leg_a.taker_fee_bps / 10000)
        cb_with_fee = cb * (1 + leg_b.taker_fee_bps / 10000)

        total_cost = ca_with_fee + cb_with_fee
        gross_payout = sz * 1.0        # one side always pays $1 per contract
        edge_usd = gross_payout - total_cost
        edge_bps = int(edge_usd / total_cost * 10000)

        if edge_bps >= min_edge_bps and (best is None or edge_bps > best["edge_bps"]):
            best = {
                "size": sz,
                "cost": total_cost,
                "edge_usd": round(edge_usd, 4),
                "edge_bps": edge_bps,
                "ca": round(ca_with_fee, 4),
                "cb": round(cb_with_fee, 4),
                "venue_a": leg_a.venue,
                "venue_b": leg_b.venue,
                "token_a": leg_a.token_id,
                "token_b": leg_b.token_id,
            }

    return best


def kelly_fraction(p: float, ask: float, cap: float = 0.25) -> float:
    """
    Kelly criterion stake fraction for a YES bet at price ask with model prob p.
    b = (1 - ask) / ask  (net odds per $ risked)
    Returns fractional Kelly (capped at cap to be conservative).
    """
    if ask <= 0 or ask >= 1 or p <= 0 or p >= 1:
        return 0.0
    b = (1.0 - ask) / ask
    q = 1.0 - p
    k = (b * p - q) / b
    return max(0.0, min(k * cap, cap))
