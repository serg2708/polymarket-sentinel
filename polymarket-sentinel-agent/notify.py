"""Telegram notifications via the main project's bot. Never raises: a failed alert must not stop trading logic."""
import html
import logging
import os
from datetime import datetime

import requests

import config as C

log = logging.getLogger("agent")
QUIET_START, QUIET_END = 22, 8      # local time: messages still arrive, but silently


def _creds():
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("ADMIN_CHAT_ID")
    env_file = C.BASE.parent / ".env"
    if (not token or not chat) and env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, val = line.partition("=")
            val = val.split("#", 1)[0].strip().strip('"').strip("'")
            if key.strip() == "TELEGRAM_BOT_TOKEN" and not token:
                token = val
            elif key.strip() == "ADMIN_CHAT_ID" and not chat:
                chat = val
    return token, chat


def send(text):
    """text is HTML; escape dynamic parts with esc()."""
    token, chat = _creds()
    if not token or not chat:
        log.warning("telegram not configured, alert dropped")
        return
    h = datetime.now().hour
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": f"🤖 <b>PolySentinel agent [{C.MODE}]</b>\n{text}"[:4096],
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "disable_notification": h >= QUIET_START or h < QUIET_END})
        if not r.ok:
            log.warning("telegram %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("telegram failed: %s", e)


def esc(s):
    return html.escape(str(s))
