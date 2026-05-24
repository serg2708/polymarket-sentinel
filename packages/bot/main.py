"""Telegram bot service entry point.

Runs two concurrent tasks:
1. aiogram polling loop — handles user commands
2. Alert consumer loop — reads from Redis alert queue and sends Telegram messages
"""
from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone, timedelta

import aiolimiter
import asyncpg
import redis.asyncio as aioredis
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..common.db import get_pool, close_pool
from ..common.metrics import start_metrics_server, ALERTS_TOTAL
from ..common.settings import get_settings
from .formatting import format_alert
from .handlers import router

log = structlog.get_logger()
settings = get_settings()

ALERT_QUEUE_KEY = "polysentinel:alerts"
# Telegram rate limits: 30 msg/sec per bot, 1 msg/sec per chat
_limiter = aiolimiter.AsyncLimiter(20, 1.0)


def _is_quiet_hours() -> bool:
    """Return True if current CET time is within quiet hours (22:00–08:00)."""
    # CET = UTC+1, CEST (summer) = UTC+2. Use UTC+2 from late March to late Oct.
    now_utc = datetime.now(timezone.utc)
    # Simple DST approximation: CEST (UTC+2) Apr–Oct, CET (UTC+1) Nov–Mar
    month = now_utc.month
    offset = 2 if 4 <= month <= 10 else 1
    now_cet = now_utc + timedelta(hours=offset)
    hour = now_cet.hour
    start = settings.quiet_hours_start   # 22
    end = settings.quiet_hours_end       # 8
    # Overnight window: 22 <= hour OR hour < 8
    return hour >= start or hour < end


def _make_bot() -> Bot:
    return Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def _make_keyboard(a: dict):
    kb = InlineKeyboardBuilder()
    if a.get("poly_url"):
        kb.button(text="Polymarket ↗", url=a["poly_url"])
    if a.get("kalshi_url"):
        kb.button(text="Kalshi ↗", url=a["kalshi_url"])
    if a.get("manifold_url"):
        kb.button(text="Manifold ↗", url=a["manifold_url"])
    group_key = a.get("group_key", "unknown")
    kb.button(text="Mute 1h", callback_data=f"mute:1h:{group_key}")
    kb.button(text="Mute forever", callback_data=f"mute:inf:{group_key}")
    kb.adjust(2, 2)
    return kb.as_markup()


async def send_alert(bot: Bot, alert: dict) -> None:
    """Send a formatted alert to the admin chat."""
    text = format_alert(alert)
    markup = _make_keyboard(alert)
    async with _limiter:
        await bot.send_message(
            settings.admin_chat_id,
            text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def alert_consumer(bot: Bot, redis_client) -> None:
    """
    Reads alert JSON from the Redis list ALERT_QUEUE_KEY and sends to Telegram.
    Checks global pause and per-group mute before sending.
    """
    log.info("alert_consumer_started")
    while True:
        try:
            # Blocking pop with 2s timeout
            item = await redis_client.blpop(ALERT_QUEUE_KEY, timeout=2)
            if not item:
                continue

            _, raw = item
            try:
                alert = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("alert_bad_json", raw=raw[:200])
                continue

            # Global pause check
            paused = await redis_client.get("global:paused")
            if paused:
                log.debug("alert_dropped_paused", kind=alert.get("kind"))
                continue

            # Per-group mute fast path (Redis)
            group_key = alert.get("group_key", "")
            muted = await redis_client.get(f"mute:{group_key}")
            if muted:
                log.debug("alert_dropped_muted", group_key=group_key)
                continue

            await send_alert(bot, alert)
            ALERTS_TOTAL.labels(kind=alert.get("kind", "unknown")).inc()
            log.info(
                "alert_sent",
                kind=alert.get("kind"),
                group_key=group_key,
                edge_bps=alert.get("edge_bps"),
            )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("alert_consumer_error", error=str(exc))
            await asyncio.sleep(1)


async def main() -> None:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )

    start_metrics_server(port=8002)

    if not settings.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN not set — bot cannot start")
        return
    if not settings.admin_chat_id:
        log.error("ADMIN_CHAT_ID not set — bot cannot start")
        return

    pool = await get_pool()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    bot = _make_bot()
    dp = Dispatcher()
    dp.include_router(router)

    # Inject dependencies into handlers via middleware
    dp["pool"] = pool
    dp["redis_client"] = redis_client

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Consumer + polling run concurrently
    consumer_task = asyncio.create_task(alert_consumer(bot, redis_client))

    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )

    log.info("bot_started", admin_chat_id=settings.admin_chat_id)

    await stop_event.wait()

    consumer_task.cancel()
    polling_task.cancel()
    await bot.session.close()
    await close_pool()
    await redis_client.aclose()
    log.info("bot_stopped")


if __name__ == "__main__":
    asyncio.run(main())
