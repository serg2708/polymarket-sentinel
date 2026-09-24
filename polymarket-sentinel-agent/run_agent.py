#!/usr/bin/env python3
"""PolySentinel agent: blind forecasts via `claude -p`, quarter-Kelly sizing, hard risk limits."""
import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import requests

import config as C
from resolve import resolve_all

GAMMA = "https://gamma-api.polymarket.com"
log = logging.getLogger("agent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions(
  id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, question TEXT,
  p_model REAL, p_market REAL, confidence REAL, reasoning TEXT, sources TEXT,
  outcome REAL);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, side TEXT, token_id TEXT,
  price REAL, stake REAL, shares REAL, mode TEXT,
  status TEXT DEFAULT 'open', pnl REAL);
"""


def db():
    con = sqlite3.connect(C.DB_PATH)
    con.executescript(SCHEMA)
    return con


def now():
    return datetime.now(timezone.utc)


def as_list(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def fetch_candidates(con):
    r = requests.get(f"{GAMMA}/markets", timeout=30, params={
        "active": "true", "closed": "false", "limit": 300,
        "order": "volume24hr", "ascending": "false"})
    r.raise_for_status()
    since = (now() - timedelta(hours=C.REEVAL_HOURS)).isoformat()
    recent = {x[0] for x in con.execute("SELECT market_id FROM predictions WHERE ts > ?", (since,))}
    held = {x[0] for x in con.execute("SELECT market_id FROM positions WHERE status='open'")}

    out = []
    for m in r.json():
        try:
            outcomes = [o.lower() for o in as_list(m.get("outcomes"))]
            prices = [float(x) for x in as_list(m.get("outcomePrices"))]
            tokens = as_list(m.get("clobTokenIds"))
            end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        mid, q = str(m["id"]), m.get("question", "")
        days = (end - now()).total_seconds() / 86400
        if outcomes != ["yes", "no"] or len(prices) != 2 or len(tokens) != 2:
            continue
        if mid in recent or mid in held or liq < C.MIN_LIQUIDITY:
            continue
        if not (C.MIN_DAYS_TO_END <= days <= C.MAX_DAYS_TO_END) or not (0.03 <= prices[0] <= 0.97):
            continue
        if any(k in q.lower() for k in C.EXCLUDE_KEYWORDS):
            continue
        out.append({"id": mid, "question": q, "description": (m.get("description") or "")[:1200],
                    "end_date": m["endDate"], "yes_price": prices[0],
                    "yes_token": tokens[0], "no_token": tokens[1]})
        if len(out) >= C.MARKETS_PER_RUN:
            break
    return out


def ask_claude(markets):
    blind = [{k: m[k] for k in ("id", "question", "description", "end_date")} for m in markets]
    prompt = (C.PROMPT_PATH.read_text()
              .replace("{{TODAY}}", now().date().isoformat())
              .replace("{{MARKETS}}", json.dumps(blind, ensure_ascii=False, indent=1)))
    cmd = [C.CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--allowedTools", "WebSearch,WebFetch", "--max-turns", str(C.CLAUDE_MAX_TURNS)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=C.CLAUDE_TIMEOUT_S)
    if res.returncode != 0:
        raise RuntimeError(f"claude exit {res.returncode}: {res.stderr[:500]}")
    env = json.loads(res.stdout)
    if env.get("is_error"):
        raise RuntimeError(f"claude error (limits?): {str(env)[:500]}")
    text = env.get("result", "")
    a, b = text.find("["), text.rfind("]")
    if a < 0 or b < 0:
        raise ValueError(f"no JSON array in output: {text[:300]}")

    ids, valid = {m["id"] for m in markets}, {}
    for d in json.loads(text[a:b + 1]):
        try:
            mid, p, c = str(d["id"]), float(d["p_yes"]), float(d["confidence"])
        except (KeyError, ValueError, TypeError):
            continue
        if mid in ids and 0.01 <= p <= 0.99 and 0 <= c <= 1:
            valid[mid] = {"p": p, "conf": c, "reasoning": str(d.get("reasoning", ""))[:1000],
                          "sources": json.dumps(d.get("sources", [])[:10])}
    return valid


def kelly_bet(p, q):
    """p = model P(YES), q = YES price. Returns (side, price, full-Kelly fraction) or None."""
    if p - q >= C.MIN_EDGE:
        return "YES", q, (p - q) / (1 - q)
    if q - p >= C.MIN_EDGE:
        return "NO", 1 - q, (q - p) / q
    return None


def risk_state(con):
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    realized = one("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE status='closed' AND mode=?", C.MODE)
    return {
        "bankroll": C.BANKROLL_USD + realized,
        "exposure": one("SELECT COALESCE(SUM(stake),0) FROM positions WHERE status='open' AND mode=?", C.MODE),
        "today": one("SELECT COALESCE(SUM(stake),0) FROM positions WHERE ts >= ? AND mode=?", midnight, C.MODE),
        "n_open": one("SELECT COUNT(*) FROM positions WHERE status='open' AND mode=?", C.MODE),
    }


def place_live(token_id, price, stake):
    # UNTESTED against your account: verify with current py-clob-client docs on a $1 order first.
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    client = ClobClient("https://clob.polymarket.com", key=os.environ["POLY_PK"], chain_id=137,
                        signature_type=int(os.getenv("POLY_SIG_TYPE", "1")),
                        funder=os.environ["POLY_FUNDER"])
    client.set_api_creds(client.create_or_derive_api_creds())
    order = client.create_order(OrderArgs(token_id=token_id, price=round(price, 2),
                                          size=round(stake / price, 2), side=BUY))
    return client.post_order(order, OrderType.GTC)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if C.KILL_SWITCH.exists():
        log.warning("KILL switch present, exiting")
        return
    con = db()
    resolve_all(con)

    st = risk_state(con)
    if st["bankroll"] < C.BANKROLL_USD * (1 - C.MAX_DRAWDOWN):
        C.KILL_SWITCH.write_text(f"drawdown stop {now().isoformat()} bankroll={st['bankroll']:.2f}\n")
        log.error("Max drawdown hit, KILL created")
        return

    markets = fetch_candidates(con)
    log.info("candidates: %d", len(markets))
    if not markets:
        return
    try:
        preds = ask_claude(markets)
    except Exception as e:  # limits, timeout, bad output -> skip this run
        log.error("claude failed: %s", e)
        return

    ts = now().isoformat()
    for m in markets:
        pr = preds.get(m["id"])
        if not pr:
            continue
        con.execute("INSERT INTO predictions(ts,market_id,question,p_model,p_market,confidence,reasoning,sources)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (ts, m["id"], m["question"], pr["p"], m["yes_price"], pr["conf"], pr["reasoning"], pr["sources"]))
        bet = kelly_bet(pr["p"], m["yes_price"])
        if not bet or pr["conf"] < C.MIN_CONFIDENCE:
            continue
        side, price, f = bet
        stake = min(st["bankroll"] * C.KELLY_FRACTION * f, st["bankroll"] * C.MAX_POSITION_FRAC)
        if stake < C.MIN_STAKE_USD:
            continue
        if st["n_open"] >= C.MAX_OPEN_POSITIONS:
            break
        if st["exposure"] + stake > st["bankroll"] * C.MAX_TOTAL_EXPOSURE_FRAC:
            continue
        if st["today"] + stake > st["bankroll"] * C.MAX_NEW_STAKE_PER_DAY_FRAC:
            continue
        token = m["yes_token"] if side == "YES" else m["no_token"]
        if C.MODE == "live":
            try:
                log.info("live order: %s", place_live(token, price, stake))
            except Exception as e:
                log.error("order failed %s: %s", m["id"], e)
                continue
        con.execute("INSERT INTO positions(ts,market_id,side,token_id,price,stake,shares,mode)"
                    " VALUES(?,?,?,?,?,?,?,?)", (ts, m["id"], side, token, price, stake, stake / price, C.MODE))
        st["exposure"] += stake
        st["today"] += stake
        st["n_open"] += 1
        log.info("[%s] %s %s @%.3f $%.2f | p=%.2f conf=%.2f | %s",
                 C.MODE, side, m["id"], price, stake, pr["p"], pr["conf"], m["question"][:80])
    con.commit()


if __name__ == "__main__":
    main()
