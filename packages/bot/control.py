"""Control panel for the host-side PolySentinel services (Claude agent, market makers).

The bot runs in Docker and cannot touch the host's systemd units, so a button only queues a
command in Redis; `polysentinel-agent/control/controller.py` (systemd: polysentinel-control) pops
it on the host, runs it and replies in this chat. Admin chat only. Starting the LIVE market maker
(real money) needs a second, explicit confirmation.
"""
from __future__ import annotations

import json
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..common.settings import get_settings

settings = get_settings()
router = Router()

CONTROL_QUEUE = "polysentinel:control"

# command -> (button label, reply shown right after the press)
COMMANDS = {
    "status":      ("📊 Статус", "Собираю статус…"),
    "mm_start":    ("▶️ Запустить MM", ""),
    "mm_stop":     ("⏹ Остановить MM", "Останавливаю маркетмейкер…"),
    "mm_kill":     ("🛑 KILL", "Аварийный стоп: снимаю заявки и блокирую запуск…"),
    "positions":   ("📈 Позиции агента", "Считаю позиции…"),
    "shadow":      ("🧮 Теневой MM", "Готовлю отчёт…"),
    "agent_run":   ("🔄 Агент сейчас", "Запускаю прогон агента (paper)…"),
    "signals":     ("🧪 Бэктест сигналов", "Считаю бэктест, до минуты…"),
}


def panel():
    kb = InlineKeyboardBuilder()
    for cmd in ("status", "positions", "mm_start", "mm_stop", "shadow", "agent_run", "signals", "mm_kill"):
        kb.button(text=COMMANDS[cmd][0], callback_data=f"ctl:{cmd}")
    kb.adjust(2)
    return kb.as_markup()


def confirm_start():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, запустить на реальные деньги", callback_data="ctl:mm_start_confirmed")
    kb.button(text="Отмена", callback_data="ctl:cancel")
    kb.adjust(1)
    return kb.as_markup()


async def enqueue(redis_client, cmd: str) -> None:
    await redis_client.rpush(CONTROL_QUEUE, json.dumps({"cmd": cmd, "ts": time.time()}))
    await redis_client.expire(CONTROL_QUEUE, 600)          # stale commands never fire later


@router.message(Command("control"))
async def cmd_control(msg: Message):
    if msg.chat.id != settings.admin_chat_id:
        return
    await msg.answer("🎛 <b>Управление PolySentinel</b>", reply_markup=panel())


@router.callback_query(lambda c: c.data and c.data.startswith("ctl:"))
async def cb_control(cb: CallbackQuery, redis_client):
    if cb.message.chat.id != settings.admin_chat_id:
        await cb.answer()
        return
    cmd = cb.data.split(":", 1)[1]
    if cmd == "mm_start":
        await cb.message.answer(
            "⚠️ <b>Запуск маркетмейкера на РЕАЛЬНЫЕ деньги.</b>\n"
            "1 рынок, до $45 в заявках, автостоп при −$15 за день. Подтвердить?",
            reply_markup=confirm_start())
        await cb.answer()
        return
    if cmd == "cancel":
        await cb.answer("Отменено")
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    if cmd == "mm_start_confirmed":
        await cb.message.edit_reply_markup(reply_markup=None)
        await enqueue(redis_client, "mm_start")
        await cb.answer("Запускаю…")
        return
    if cmd not in COMMANDS:
        await cb.answer("Неизвестная команда")
        return
    await enqueue(redis_client, cmd)
    await cb.answer(COMMANDS[cmd][1] or "Принято")
