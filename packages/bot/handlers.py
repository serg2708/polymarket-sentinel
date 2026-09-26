"""Telegram command handlers for PolySentinel bot."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import redis.asyncio as aioredis
import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..common.settings import get_settings
from .formatting import e

log = structlog.get_logger()
settings = get_settings()
router = Router()


def _guard(msg: Message) -> bool:
    """Return True if message is from the admin chat."""
    return msg.chat.id == settings.admin_chat_id


def _guard_cb(cb: CallbackQuery) -> bool:
    return cb.message.chat.id == settings.admin_chat_id


# ── /start ────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(msg: Message):
    if not _guard(msg):
        return
    await msg.answer(
        "<b>PolySentinel</b> is online 🟢\n\n"
        "Commands:\n"
        "/status — ingest health + last alert\n"
        "/positions — current Polymarket portfolio\n"
        "/list — watched markets + thresholds\n"
        "/watch &lt;slug&gt; — add a market to watchlist\n"
        "/unwatch &lt;slug&gt; — remove from watchlist\n"
        "/threshold arb &lt;bps&gt; — set arb alert threshold\n"
        "/threshold soft &lt;bps&gt; — set soft-edge threshold\n"
        "/pause &lt;duration&gt; — pause alerts (e.g. 2h, 30m)\n"
        "/resume — resume paused alerts\n"
        "/calibration — show model calibration stats\n"
        "/explain &lt;alert_id&gt; — verbose alert breakdown\n"
        "/control — панель: агент, маркетмейкер (старт/стоп/KILL)\n",
    )


# ── /positions ────────────────────────────────────────────────────────────

@router.message(Command("positions"))
async def cmd_positions(msg: Message):
    if not _guard(msg):
        return
    from ..ingest.polymarket_clob import get_positions, get_open_orders, get_portfolio_value
    positions, orders, portfolio_value = await asyncio.gather(
        get_positions(), get_open_orders(), get_portfolio_value()
    )

    value_str = f"  Portfolio: <code>${portfolio_value:.2f}</code>" if portfolio_value is not None else ""
    lines = [f"<b>Polymarket Portfolio</b>{value_str}"]

    if not positions:
        lines.append("No open positions (or address not configured).")
    else:
        total_value = 0.0
        total_cost = 0.0
        for p in positions:
            title = (p.get("title") or p.get("market") or p.get("asset") or "?")[:45]
            outcome = p.get("outcome", "?")
            size = float(p.get("size") or p.get("shares") or 0)
            avg_price = float(p.get("avgPrice") or p.get("averagePrice") or 0)
            cur_value = float(p.get("currentValue") or 0) or (size * avg_price)
            cur_price = float(p.get("currentPrice") or (cur_value / size if size > 0 else avg_price))
            cost = size * avg_price
            pnl = cur_value - cost
            pnl_sign = "+" if pnl >= 0 else ""
            total_value += cur_value
            total_cost += cost
            lines.append(
                f"\n<b>{e(title)}</b> [{e(outcome)}]\n"
                f"  {size:.0f} shares @ {avg_price:.3f}  →  now {cur_price:.3f}\n"
                f"  Value: <code>${cur_value:.2f}</code>  P&L: <code>{pnl_sign}${pnl:.2f}</code>"
            )
        total_pnl = total_value - total_cost
        pnl_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n<b>Total value: ${total_value:.2f}  P&L: {pnl_sign}${total_pnl:.2f}</b>")

    if orders:
        lines.append(f"\n<b>Open orders: {len(orders)}</b>")
        for o in orders[:5]:
            side = o.get("side", "?")
            size = float(o.get("original_size") or o.get("size") or 0)
            price = float(o.get("price") or 0)
            lines.append(f"  {side} {size:.0f} @ {price:.3f}")

    await msg.answer("\n".join(lines))


# ── /status ───────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(msg: Message, pool: asyncpg.Pool, redis_client):
    if not _guard(msg):
        return

    # Last alert
    row = await pool.fetchrow(
        "SELECT ts, kind, group_key, edge_bps FROM alerts ORDER BY ts DESC LIMIT 1"
    )
    if row:
        last = f"{row['kind']} ({row['edge_bps']} bps) @ {row['ts'].strftime('%H:%M UTC')}"
    else:
        last = "never"

    # DB price freshness
    fresh = await pool.fetchrow(
        "SELECT count(*) AS n, max(ts) AS latest FROM prices WHERE ts > now() - INTERVAL '2 minutes'"
    )
    n_fresh = fresh["n"] if fresh else 0
    latest_ts = fresh["latest"].strftime("%H:%M:%S UTC") if fresh and fresh["latest"] else "—"

    # Redis ping
    try:
        await redis_client.ping()
        redis_ok = "✅"
    except Exception:
        redis_ok = "❌"

    await msg.answer(
        f"<b>PolySentinel Status</b>\n"
        f"Price ticks (2min): <code>{n_fresh}</code>  latest: <code>{latest_ts}</code>\n"
        f"Redis: {redis_ok}\n"
        f"Last alert: <code>{e(last)}</code>\n"
    )


# ── /list ─────────────────────────────────────────────────────────────────

@router.message(Command("list"))
async def cmd_list(msg: Message, pool: asyncpg.Pool, redis_client):
    if not _guard(msg):
        return

    matches = await pool.fetch(
        """
        SELECT group_key, string_agg(source || ':' || source_id, ' | ') AS legs
        FROM market_matches
        WHERE approved_by IN ('auto','manual')
        GROUP BY group_key
        ORDER BY group_key
        """
    )

    arb_thresh = await redis_client.get("th:arb:global") or str(settings.arb_min_edge_bps)
    soft_thresh = await redis_client.get("th:soft:global") or str(settings.soft_edge_min_bps)

    header = f"<b>Approved pairs ({len(matches)}):</b>"
    footer = f"\nThresholds: arb <code>{arb_thresh} bps</code>  soft <code>{soft_thresh} bps</code>"

    # Split into pages of ≤4000 chars to stay within Telegram message limit
    page: list[str] = [header]
    page_len = len(header)
    for i, row in enumerate(matches):
        line = f"  • <code>{e(row['group_key'])}</code>  {e(row['legs'])}"
        is_last = i == len(matches) - 1
        extra = footer if is_last else ""
        if page_len + len(line) + len(extra) + 2 > 4000 and len(page) > 1:
            await msg.answer("\n".join(page))
            page = []
            page_len = 0
        page.append(line)
        page_len += len(line) + 1

    page.append(footer)
    await msg.answer("\n".join(page))


# ── /watch ────────────────────────────────────────────────────────────────

@router.message(Command("watch"))
async def cmd_watch(msg: Message, pool: asyncpg.Pool):
    if not _guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /watch &lt;polymarket_slug_or_group_key&gt;")
        return
    slug = parts[1].strip()
    # Check if market exists in DB
    row = await pool.fetchrow(
        "SELECT market_id, question FROM markets WHERE slug=$1 OR market_id=$1 LIMIT 1", slug
    )
    if not row:
        await msg.answer(f"Market <code>{e(slug)}</code> not found in DB. Has ingest run yet?")
        return
    await msg.answer(f"✅ Watching <code>{e(slug)}</code>: {e(row['question'])}")


# ── /unwatch ──────────────────────────────────────────────────────────────

@router.message(Command("unwatch"))
async def cmd_unwatch(msg: Message):
    if not _guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /unwatch &lt;group_key&gt;")
        return
    await msg.answer(f"Removed <code>{e(parts[1].strip())}</code> from watchlist.")


# ── /threshold ────────────────────────────────────────────────────────────

@router.message(Command("threshold"))
async def cmd_threshold(msg: Message, redis_client):
    if not _guard(msg):
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer("Usage: /threshold arb &lt;bps&gt;  or  /threshold soft &lt;bps&gt;")
        return
    kind, value = parts[1].lower(), parts[2]
    try:
        bps = int(value)
    except ValueError:
        await msg.answer("Value must be an integer (basis points).")
        return
    await redis_client.set(f"th:{kind}:global", str(bps))
    await msg.answer(f"✅ <b>{e(kind)}</b> threshold set to <code>{bps} bps</code>")


# ── /pause / /resume ──────────────────────────────────────────────────────

@router.message(Command("pause"))
async def cmd_pause(msg: Message, redis_client):
    if not _guard(msg):
        return
    parts = (msg.text or "").split()
    raw = parts[1] if len(parts) > 1 else "4h"
    seconds = _parse_duration(raw)
    if seconds is None:
        await msg.answer("Usage: /pause 2h  or  /pause 30m  or  /pause 1d")
        return
    await redis_client.set("global:paused", "1", ex=seconds)
    await msg.answer(f"⏸ Alerts paused for <code>{raw}</code>.")


@router.message(Command("resume"))
async def cmd_resume(msg: Message, redis_client):
    if not _guard(msg):
        return
    await redis_client.delete("global:paused")
    await msg.answer("▶️ Alerts resumed.")


# ── /calibration ──────────────────────────────────────────────────────────

@router.message(Command("calibration"))
async def cmd_calibration(msg: Message, pool: asyncpg.Pool):
    if not _guard(msg):
        return
    rows = await pool.fetch(
        """
        SELECT
          model_kind,
          count(*) FILTER (WHERE outcome IS NOT NULL) AS resolved,
          avg((model_p - outcome)^2) FILTER (WHERE outcome IS NOT NULL) AS brier,
          count(*) AS total
        FROM calibration_snapshots
        WHERE ts > now() - INTERVAL '30 days'
        GROUP BY model_kind
        ORDER BY brier ASC NULLS LAST
        """
    )
    if not rows:
        await msg.answer("No calibration data yet (markets need to resolve).")
        return

    lines = ["<b>Model calibration (30d):</b>"]
    for r in rows:
        brier = f"{r['brier']:.4f}" if r["brier"] else "n/a"
        lines.append(
            f"  <code>{e(r['model_kind'])}</code>: Brier <code>{brier}</code>  "
            f"resolved: <code>{r['resolved']}/{r['total']}</code>"
        )
    await msg.answer("\n".join(lines))


# ── /explain ──────────────────────────────────────────────────────────────

@router.message(Command("explain"))
async def cmd_explain(msg: Message, pool: asyncpg.Pool):
    if not _guard(msg):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /explain &lt;alert_id&gt;")
        return
    alert_id = parts[1].strip()
    row = await pool.fetchrow(
        "SELECT * FROM alerts WHERE id=$1 OR id::text LIKE $2 LIMIT 1",
        alert_id, f"{alert_id}%"
    )
    if not row:
        await msg.answer(f"Alert <code>{e(alert_id)}</code> not found.")
        return
    payload = row["payload"] or {}
    text = (
        f"<b>Alert {e(str(row['id'])[:8])}</b>\n"
        f"Kind: <code>{e(row['kind'])}</code>  "
        f"Edge: <code>{row['edge_bps']} bps</code>\n"
        f"Group: <code>{e(row['group_key'])}</code>\n"
        f"Time: <code>{row['ts'].strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n"
        f"<pre>{e(json.dumps(payload, indent=2)[:1000])}</pre>"
    )
    await msg.answer(text)


# ── Callback: mute button ─────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("mute:"))
async def cb_mute(cb: CallbackQuery, pool: asyncpg.Pool, redis_client):
    if not _guard_cb(cb):
        await cb.answer("Unauthorized")
        return

    _, duration, group_key = cb.data.split(":", 2)
    seconds = _parse_duration(duration) if duration != "inf" else 86400 * 365

    if seconds:
        # Write to DB mutes table
        muted_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await pool.execute(
            """
            INSERT INTO mutes (group_key, muted_until)
            VALUES ($1, $2)
            ON CONFLICT (group_key) DO UPDATE SET muted_until=$2
            """,
            group_key, muted_until
        )
        # Also set Redis key for fast path
        await redis_client.set(f"mute:{group_key}", "1", ex=seconds)

    label = duration if duration != "inf" else "forever"
    await cb.answer(f"Muted {group_key} for {label}")
    await cb.message.edit_reply_markup(reply_markup=None)


# ── Helpers ───────────────────────────────────────────────────────────────

def _parse_duration(s: str) -> int | None:
    s = s.lower().strip()
    try:
        if s.endswith("s"):
            return int(s[:-1])
        if s.endswith("m"):
            return int(s[:-1]) * 60
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        return int(s)  # bare seconds
    except ValueError:
        return None
