"""Score the bot's own predictions against realised market outcomes.

Without this step the feedback loop is open: `calibration_snapshots.outcome`
stays NULL and `alerts.status` stays 'pending' forever, so hit-rate and
calibration cannot be computed and no signal type can be judged on evidence.

The Polymarket market WS channel only emits book/price events, so the
`market_resolved` branch in `poly_ws_handler` never fires. We poll Gamma
instead: a settled market has `closed=true`, `umaResolutionStatus='resolved'`
and `outcomePrices` pinned to ["1","0"] (YES won) or ["0","1"] (NO won).
"""
from __future__ import annotations

import asyncio
import json

import asyncpg
import structlog

from .polymarket import GAMMA, _client
from ..models.calibration import mark_resolved

log = structlog.get_logger()

# Prices are strings and settled markets pin them to the rails; allow a little
# slack so a market that settled at 0.999/0.001 still counts.
_SETTLED_EPS = 0.02


def parse_resolution(m: dict) -> int | None:
    """Return 1 if the market settled YES, 0 if NO, None if not settled yet.

    Guards against half-settled records: a market flagged closed but still
    showing a mid-range price has not actually paid out, and scoring it would
    poison the calibration set.
    """
    if not m.get("closed"):
        return None
    status = str(m.get("umaResolutionStatus") or "").lower()
    if status and status != "resolved":
        return None

    raw = m.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        yes, no = float(prices[0]), float(prices[1])
    except (TypeError, ValueError, IndexError, json.JSONDecodeError):
        return None

    if yes >= 1 - _SETTLED_EPS and no <= _SETTLED_EPS:
        return 1
    if no >= 1 - _SETTLED_EPS and yes <= _SETTLED_EPS:
        return 0
    return None


async def fetch_resolution(market_id: str) -> int | None:
    """Look one market up on Gamma and return its settled outcome, or None."""
    try:
        r = await _client().get(f"{GAMMA}/markets/{market_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse_resolution(r.json())
    except Exception as exc:
        log.warning("resolution_fetch_error", market_id=market_id, error=str(exc))
        return None


def _alert_market_id(kind: str, group_key: str, payload: dict) -> str | None:
    """Recover the market id an alert was about.

    tail_risk and news_divergence carry it in the payload; soft_edge_llm_prior
    does not, but its group_key is "llm_prior:<market_id>".
    """
    mid = payload.get("market_id")
    if mid:
        return str(mid)
    if group_key and ":" in group_key:
        tail = group_key.split(":", 1)[1].strip()
        if tail.isdigit():
            return tail
    return None


def _alert_side_is_yes(payload: dict) -> bool | None:
    """True when the alert said BUY YES, False for BUY NO, None if unclear.

    tail_risk only ever fires BUY YES; llm_prior fires both, and its side is
    the sign of (model estimate - market ask).
    """
    if payload.get("kind") == "tail_risk" or "claude_p" in payload:
        return True

    # news_divergence states its side outright.
    direction = str(payload.get("direction") or "")
    if direction.startswith("YES"):
        return True
    if direction.startswith("NO"):
        return False

    model_p, ask = payload.get("model_p"), payload.get("market_ask")
    if model_p is None or ask is None:
        return None
    try:
        return float(model_p) > float(ask)
    except (TypeError, ValueError):
        return None


async def _pending_market_ids(pool: asyncpg.Pool) -> tuple[dict[str, list[str]], list[asyncpg.Record]]:
    """Collect what still needs scoring.

    Returns (market_id -> snapshot keys, pending alert rows). Most snapshot
    keys are the market id itself; polymarket_baseline rows are keyed by
    token_id instead, so they are mapped back through the tokens table.
    """
    keys: dict[str, list[str]] = {}

    direct = await pool.fetch(
        """
        SELECT DISTINCT market_id FROM calibration_snapshots
        WHERE outcome IS NULL AND model_kind <> 'polymarket_baseline'
        """
    )
    for row in direct:
        keys.setdefault(row["market_id"], []).append(row["market_id"])

    baseline = await pool.fetch(
        """
        SELECT DISTINCT c.market_id AS token_id, t.market_id
        FROM calibration_snapshots c
        JOIN tokens t ON t.token_id = c.market_id
        WHERE c.outcome IS NULL AND c.model_kind = 'polymarket_baseline'
        """
    )
    for row in baseline:
        keys.setdefault(row["market_id"], []).append(row["token_id"])

    alerts = await pool.fetch(
        "SELECT id, kind, group_key, payload FROM alerts WHERE status = 'pending'"
    )
    return keys, alerts


async def resolve_pending_outcomes(pool: asyncpg.Pool, limit: int = 500) -> dict:
    """Label settled markets in calibration_snapshots and alerts.

    Safe to re-run: only rows still NULL/'pending' are touched, and a market
    that has not settled yet is simply skipped until next time.
    """
    keys, alerts = await _pending_market_ids(pool)

    wanted: set[str] = set(keys)
    alert_targets: list[tuple] = []
    for a in alerts:
        payload = a["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        mid = _alert_market_id(a["kind"], a["group_key"] or "", payload)
        if mid:
            wanted.add(mid)
            alert_targets.append((a["id"], mid, _alert_side_is_yes(payload)))

    todo = sorted(wanted)[:limit]
    log.info("resolution_scan_start", markets=len(todo), pending_alerts=len(alert_targets))

    outcomes: dict[str, int] = {}
    for mid in todo:
        got = await fetch_resolution(mid)
        if got is not None:
            outcomes[mid] = got
        await asyncio.sleep(0.12)  # be polite to Gamma

    snapshots_labelled = 0
    for mid, outcome in outcomes.items():
        for key in keys.get(mid, []):
            await mark_resolved(pool, key, outcome)
            snapshots_labelled += 1

    won = lost = 0
    for alert_id, mid, side_is_yes in alert_targets:
        if mid not in outcomes or side_is_yes is None:
            continue
        yes_won = outcomes[mid] == 1
        status = "won" if yes_won == side_is_yes else "lost"
        await pool.execute("UPDATE alerts SET status = $1 WHERE id = $2", status, alert_id)
        won += status == "won"
        lost += status == "lost"

    log.info("resolution_scan_done", checked=len(todo), settled=len(outcomes),
             snapshots_labelled=snapshots_labelled, alerts_won=won, alerts_lost=lost)
    return {"checked": len(todo), "settled": len(outcomes),
            "snapshots": snapshots_labelled, "won": won, "lost": lost}
