#!/usr/bin/env python3
"""Runs the /control commands the Telegram bot queues in Redis, and replies in Telegram.

The bot lives in Docker and cannot reach the host's systemd user units; this process can.
systemd: polysentinel-control.service. Redis is bound to 127.0.0.1 by docker-compose, and only the
admin chat can press the buttons (checked in packages/bot/control.py).
"""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
sys.path.insert(0, str(AGENT))
from notify import esc, send  # noqa: E402

QUEUE = "polysentinel:control"
PY = str(AGENT / ".venv" / "bin" / "python")
KILL = AGENT / "mm" / "KILL"
MAX_AGE_S = 120                  # a press older than this (e.g. queued while we were down) is ignored
LIVE_UNIT, SHADOW_UNIT, AGENT_UNIT = "polysentinel-mm-live", "polysentinel-mm", "polysentinel-agent"

log = logging.getLogger("control")


def sh(args, timeout=180):
    try:
        r = subprocess.run(args, cwd=AGENT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 1, f"timeout after {timeout}s"


def systemctl(*args):
    return sh(["systemctl", "--user", *args], timeout=60)


def active(unit):
    return systemctl("is-active", unit)[1].strip() or "unknown"


def clean(out, keep=25):
    lines = [l for l in out.splitlines() if l.strip() and "RuntimeWarning" not in l and "HTTP Request" not in l]
    return "\n".join(lines[-keep:])


def head(out, keep):
    lines = [l for l in out.splitlines() if l.strip() and "RuntimeWarning" not in l and "HTTP Request" not in l]
    return "\n".join(lines[:keep])


def exchange_line():
    """Read-only account snapshot; empty if no secrets are configured."""
    if not os.getenv("POLY_PK"):
        return "exchange: no secrets loaded"
    try:
        from polymarket import SecureClient
        sc = SecureClient.create(private_key=os.environ["POLY_PK"], wallet=os.environ["POLY_FUNDER"])
        cash = int(sc.get_balance_allowance(asset_type="COLLATERAL").balance) / 1e6
        orders = len(list(sc.list_open_orders().iter_items()))
        return f"exchange: cash ${cash:.2f}, open orders {orders}"
    except Exception as e:
        return f"exchange: error {str(e)[:120]}"


def status():
    nxt = systemctl("show", f"{AGENT_UNIT}.timer", "-p", "NextElapseUSecRealtime", "--value")[1].strip() or "?"
    lines = [f"live MM:    {active(LIVE_UNIT)}{'  (KILL file present)' if KILL.exists() else ''}",
             f"shadow MM:  {active(SHADOW_UNIT)}",
             f"agent:      timer {active(AGENT_UNIT + '.timer')}, next run {nxt}",
             exchange_line(), ""]
    lines.append(head(sh([PY, "mm/shadow_mm.py", "--report"])[1], 4))     # the summary is at the top
    lines.append("")
    lines.append(clean(sh([PY, "resolve.py", "--positions"])[1], 3))
    return "\n".join(lines)


def run(cmd):
    if cmd == "status":
        return status()
    if cmd == "mm_start":
        if KILL.exists():
            KILL.unlink()                                   # the press was confirmed in Telegram
        rc, out = systemctl("start", LIVE_UNIT)
        time.sleep(8)
        return f"start rc={rc} {clean(out)}\nlive MM: {active(LIVE_UNIT)}"
    if cmd == "mm_stop":
        rc, out = systemctl("stop", LIVE_UNIT)             # SIGTERM -> bot cancels its orders
        return f"stop rc={rc} {clean(out)}\nlive MM: {active(LIVE_UNIT)}"
    if cmd == "mm_kill":
        KILL.write_text(f"{time.strftime('%F %T')} KILL from Telegram\n")
        for _ in range(30):                                  # the bot polls KILL every 15 s
            if active(LIVE_UNIT) != "active":
                break
            time.sleep(1)
        if active(LIVE_UNIT) == "active":
            systemctl("stop", LIVE_UNIT)
        return f"KILL set; live MM: {active(LIVE_UNIT)}. Start again with ▶️ (it clears KILL)."
    if cmd == "positions":
        return clean(sh([PY, "resolve.py", "--positions"])[1])
    if cmd == "shadow":
        return clean(sh([PY, "mm/shadow_mm.py", "--report"])[1])
    if cmd == "agent_run":
        rc, out = systemctl("start", "--no-block", f"{AGENT_UNIT}.service")
        return f"agent run started (rc={rc}); new trades arrive as usual alerts"
    if cmd == "signals":
        return clean(sh([PY, "backtest_signals.py"], timeout=900)[1])
    return f"unknown command {cmd!r}"


def handle(raw, now=None):
    """One queued item -> reply text, or None if it must be ignored."""
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    if (now or time.time()) - float(item.get("ts") or 0) > MAX_AGE_S:
        log.warning("stale command ignored: %s", item)
        return None
    cmd = str(item.get("cmd"))
    log.info("command %s", cmd)
    return f"🎛 <b>{esc(cmd)}</b>\n<pre>{esc(run(cmd))}</pre>"


def main():
    import redis
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = redis.from_url(os.getenv("CONTROL_REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    log.info("control loop started")
    while True:
        try:
            got = r.blpop(QUEUE, timeout=5)
        except Exception as e:                                # redis down (docker stopped): retry
            log.warning("redis: %s", e)
            time.sleep(10)
            continue
        if not got:
            continue
        try:
            reply = handle(got[1])
        except Exception as e:
            log.exception("command failed")
            reply = f"🎛 command failed: <code>{esc(str(e)[:300])}</code>"
        if reply:
            send(reply[:4000], tag="PolySentinel control")


if __name__ == "__main__":
    main()
