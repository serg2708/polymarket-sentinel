#!/usr/bin/env python3
"""Backtest of the main bot's LLM signals: `python backtest_signals.py [--notify]`.

For each market that got a signal, take the FIRST alert, assume a $1 buy of the side the
signal pointed to at the alert's price, and settle it against the resolved outcome.
Alerts are read from the main project's Postgres through `docker compose exec`.
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

import config as C

COMPOSE = ["docker", "compose", "-f", str(C.BASE.parent / "docker-compose.yml")]
GAMMA = "https://gamma-api.polymarket.com"
CACHE = C.BASE / "reports" / "outcomes_cache.json"   # resolved outcomes never change
RECENT_FROM = "2026-09-26"   # signals after the watch-only / rules-and-siblings fixes

SQL = """
SELECT DISTINCT ON (kind, mid) kind, mid, ts::date, price, model_p, direction, conf FROM (
  SELECT kind, ts,
    COALESCE(payload->>'market_id', split_part(payload->>'group_key', ':', 2)) AS mid,
    COALESCE(payload->>'market_ask', payload->>'market_p') AS price,
    COALESCE(payload->>'model_p', payload->>'claude_p', '') AS model_p,
    COALESCE(payload->>'direction', '') AS direction,
    COALESCE(payload->>'confidence', '') AS conf
  FROM alerts WHERE kind IN ('tail_risk', 'soft_edge_llm_prior', 'news_divergence')) x
ORDER BY kind, mid, ts
"""


def alerts():
    out = subprocess.run(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "polysentinel",
                                    "-At", "-F", "\t", "-c", SQL],
                         capture_output=True, text=True, timeout=120, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line.strip()]


def outcome(mid):
    """1 / 0 once resolved, None while open. /markets/{id} includes closed markets."""
    try:
        m = requests.get(f"{GAMMA}/markets/{mid}", timeout=20).json()
        p = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        if m.get("closed") and len(p) == 2 and max(p) >= 0.99:
            return 1 if p[0] > p[1] else 0
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    return None


def outcomes(mids):
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    todo = [m for m in mids if m not in cache]
    with ThreadPoolExecutor(12) as ex:
        for mid, o in zip(todo, ex.map(outcome, todo)):
            if o is not None:
                cache[mid] = o
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    return cache


def bets(rows, res):
    out = []
    for kind, mid, day, price, model_p, direction, conf in rows:
        if mid not in res or not price:
            continue
        price = float(price)
        if kind == "soft_edge_llm_prior":
            yes = float(model_p or 0) > price
        elif kind == "news_divergence":
            yes = direction == "YES_underpriced"
        else:                                   # tail_risk is always BUY YES
            yes = True
        cost = price if yes else 1 - price
        if not 0 < cost < 1:
            continue
        out.append({"kind": kind, "day": day, "cost": cost, "won": res[mid] == (1 if yes else 0),
                    "conf": float(conf) if conf else None})
    return out


def line(name, bs):
    if not bs:
        return f"{name:24} n=0"
    n = len(bs)
    won = sum(b["won"] for b in bs)
    pnl = sum((1 / b["cost"] if b["won"] else 0) - 1 for b in bs)
    implied = sum(b["cost"] for b in bs) / n
    return (f"{name:24} n={n:3} won {won / n:4.0%} (mkt {implied:4.0%})  "
            f"ROI {pnl / n:+5.0%}  $10 each {pnl * 10:+5.0f}$")


def report():
    rows = alerts()
    res = outcomes(sorted({r[1] for r in rows}))
    bs = bets(rows, res)
    lines = [f"Main-bot LLM signals: {len(bs)} of {len(rows)} signalled markets resolved",
             "ROI = profit per $1 staked, buying the signalled side at alert price", ""]
    for kind in ("tail_risk", "soft_edge_llm_prior", "news_divergence"):
        lines.append(line(kind, [b for b in bs if b["kind"] == kind]))
    lines.append(line("ALL", bs))
    lines += ["", f"Since {RECENT_FROM} (after fixes):",
              line("ALL recent", [b for b in bs if b["day"] >= RECENT_FROM])]
    return "\n".join(lines)


if __name__ == "__main__":
    text = report()
    print(text)
    if "--notify" in sys.argv:
        from notify import esc, send
        send(f"🧪 <b>Main-bot signals backtest</b>\n<pre>{esc(text)}</pre>")
