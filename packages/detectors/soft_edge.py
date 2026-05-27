"""Soft-edge EV detector.

Compares model probability (from Metaculus, Manifold, or a local model)
against the Polymarket ask price and fires if the EV signal exceeds threshold.
"""
from __future__ import annotations

import math

from ..detectors.arb_xplatform import kelly_fraction


def compute_ev(model_p: float, market_ask: float) -> dict | None:
    """
    EV for the best side to trade given model probability vs Polymarket mid.

    BUY YES when model_p > market_ask  → positive ev_per_dollar, Kelly for YES.
    BUY NO  when model_p < market_ask  → negative ev_per_dollar, Kelly for NO.
    Kelly is always computed for the actionable side (never negative).
    """
    if market_ask <= 0 or market_ask >= 1 or model_p <= 0 or model_p >= 1:
        return None

    if model_p >= market_ask:
        ev_per_dollar = (model_p - market_ask) / market_ask
        kf = kelly_fraction(model_p, market_ask)
    else:
        # BUY NO: pay ~(1 - market_ask) per NO contract, wins $1 if NO resolves.
        # Approximate NO ask as complement of YES ask (valid when spread is tight).
        no_ask = 1.0 - market_ask
        no_prob = 1.0 - model_p
        ev_per_dollar = (no_prob - no_ask) / no_ask  # positive, from NO perspective
        kf = kelly_fraction(no_prob, no_ask)

    return {
        "model_p": round(model_p, 4),
        "market_ask": round(market_ask, 4),
        "edge_pp": round((model_p - market_ask) * 100, 2),
        "ev_per_dollar": round(ev_per_dollar, 4),
        "kelly_fraction": round(kf, 4),
    }


def edge_bps_soft(model_p: float, market_ask: float) -> int:
    """Return edge in basis points — computed on the actionable side (YES or NO)."""
    if market_ask <= 0 or market_ask >= 1:
        return 0
    if model_p >= market_ask:
        raw = (model_p - market_ask) / market_ask
    else:
        no_ask = 1.0 - market_ask
        if no_ask <= 0:
            return 0
        raw = ((1.0 - model_p) - no_ask) / no_ask
    return max(0, int(raw * 10000))


def metaculus_soft_edge(
    metaculus_median: float | None,
    poly_ask: float | None,
    min_edge_bps: int = 300,
    min_edge_pp: float = 5.0,
) -> dict | None:
    """Fire when Metaculus community median diverges meaningfully from Polymarket."""
    if metaculus_median is None or poly_ask is None:
        return None
    if abs(metaculus_median - poly_ask) * 100 < min_edge_pp:
        return None
    edge = compute_ev(metaculus_median, poly_ask)
    if edge and edge_bps_soft(metaculus_median, poly_ask) >= min_edge_bps:
        return {**edge, "kind": "soft_edge_metaculus", "source": "metaculus"}
    return None


def llm_prior_soft_edge(
    llm_p: float | None,
    poly_ask: float | None,
    min_edge_bps: int = 500,
    min_edge_pp: float = 5.0,
) -> dict | None:
    """Fire when Claude's probability estimate diverges from Polymarket.

    Both BUY YES and BUY NO signals are kept — LLM is not systematically
    biased in one direction the way Manifold play-money is.
    Higher threshold than Metaculus: LLM estimates have wider error bars.
    """
    if llm_p is None or poly_ask is None:
        return None
    if abs(llm_p - poly_ask) * 100 < min_edge_pp:
        return None
    edge = compute_ev(llm_p, poly_ask)
    if edge and edge_bps_soft(llm_p, poly_ask) >= min_edge_bps:
        return {**edge, "kind": "soft_edge_llm_prior", "source": "llm_prior"}
    return None


def predictit_soft_edge(
    predictit_yes_ask: float | None,
    poly_ask: float | None,
    min_edge_bps: int = 300,
    min_edge_pp: float = 3.0,
) -> dict | None:
    """Fire when PredictIt price diverges meaningfully from Polymarket.

    PredictIt is real money → both BUY YES and BUY NO signals are kept.
    10% profit fee is already baked into the threshold (we require 300+ bps to clear fees).
    """
    if predictit_yes_ask is None or poly_ask is None:
        return None
    if abs(predictit_yes_ask - poly_ask) * 100 < min_edge_pp:
        return None
    edge = compute_ev(predictit_yes_ask, poly_ask)
    if edge and edge_bps_soft(predictit_yes_ask, poly_ask) >= min_edge_bps:
        return {**edge, "kind": "soft_edge_predictit", "source": "predictit"}
    return None


def manifold_soft_edge(
    manifold_prob: float | None,
    poly_ask: float | None,
    min_edge_bps: int = 500,
    min_edge_pp: float = 5.0,
) -> dict | None:
    """Fire when Manifold thinks something is MORE likely than Polymarket prices.

    Only BUY YES signals are kept. BUY NO signals are suppressed entirely:
    Manifold play-money systematically underestimates low-probability events,
    making almost every Polymarket price look "overpriced" — that's bias, not edge.
    """
    if manifold_prob is None or poly_ask is None:
        return None
    # Suppress BUY NO: Manifold's downward bias on low-prob events is not actionable
    if manifold_prob < poly_ask:
        return None
    if (manifold_prob - poly_ask) * 100 < min_edge_pp:
        return None
    edge = compute_ev(manifold_prob, poly_ask)
    if edge and edge_bps_soft(manifold_prob, poly_ask) >= min_edge_bps:
        # Halve Kelly: play-money signal is weaker than real-money source
        scaled = {**edge, "kelly_fraction": round(edge["kelly_fraction"] * 0.5, 4)}
        return {**scaled, "kind": "soft_edge_manifold", "source": "manifold"}
    return None
