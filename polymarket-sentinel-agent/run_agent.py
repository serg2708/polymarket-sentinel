#!/usr/bin/env python3
"""PolySentinel agent: blind forecasts via `claude -p`, quarter-Kelly sizing on executable prices, hard risk limits."""
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import config as C
from notify import esc, market_url, send
from resolve import resolve_all

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
log = logging.getLogger("agent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions(
  id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, question TEXT,
  p_model REAL, p_market REAL, confidence REAL, reasoning TEXT, sources TEXT,
  outcome REAL, leaked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, side TEXT, token_id TEXT,
  price REAL, stake REAL, shares REAL, mode TEXT,
  status TEXT DEFAULT 'open', pnl REAL, fee REAL DEFAULT 0, order_id TEXT);
"""
MIGRATIONS = [("predictions", "leaked", "INTEGER DEFAULT 0"),
              ("positions", "fee", "REAL DEFAULT 0"),
              ("positions", "order_id", "TEXT")]
EXCLUDE_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in C.EXCLUDE_KEYWORDS) + r")\b", re.I)


def db():
    con = sqlite3.connect(C.DB_PATH)
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # already there
    return con


def now():
    return datetime.now(timezone.utc)


def as_list(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def fetch_candidates(con):
    pool = []
    for offset in range(0, C.CANDIDATE_POOL, 100):      # gamma caps a page at 100
        r = requests.get(f"{GAMMA}/markets", timeout=30, params={
            "active": "true", "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"})
        r.raise_for_status()
        page = r.json()
        pool += page
        if len(page) < 100:
            break
    since = (now() - timedelta(hours=C.REEVAL_HOURS)).isoformat()
    recent = {x[0] for x in con.execute("SELECT market_id FROM predictions WHERE ts > ?", (since,))}
    seen = {x[0] for x in con.execute("SELECT market_id FROM predictions")}
    held = {x[0] for x in con.execute("SELECT market_id FROM positions WHERE status='open'")}

    fresh, again = [], []
    for m in pool:
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
        if not m.get("enableOrderBook", True) or m.get("acceptingOrders") is False:
            continue
        if mid in recent or mid in held or liq < C.MIN_LIQUIDITY:
            continue
        if not (C.MIN_DAYS_TO_END <= days <= C.MAX_DAYS_TO_END) or not (0.03 <= prices[0] <= 0.97):
            continue
        if EXCLUDE_RE.search(q):
            continue
        (again if mid in seen else fresh).append(
            {"id": mid, "question": q, "description": (m.get("description") or "")[:C.MAX_DESCRIPTION_CHARS],
             "end_date": m["endDate"], "yes_price": prices[0],
             "yes_token": tokens[0], "no_token": tokens[1], "url": market_url(m)})
    # Top-by-volume markets are the most efficient and barely change run to run: sample instead,
    # never-forecast markets first.
    random.shuffle(fresh)
    random.shuffle(again)
    return (fresh + again)[:C.MARKETS_PER_RUN]


def ask_claude(markets):
    blind = [{k: m[k] for k in ("id", "question", "description", "end_date")} for m in markets]
    prompt = (C.PROMPT_PATH.read_text()
              .replace("{{TODAY}}", now().date().isoformat())
              .replace("{{MARKETS}}", json.dumps(blind, ensure_ascii=False, indent=1)))
    blocked = [f"WebFetch(domain:{d})" for d in C.PRICE_LEAK_DOMAINS if "." in d]
    cmd = [C.CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--allowedTools", "WebSearch,WebFetch", "--disallowedTools", ",".join(blocked),
           "--max-turns", str(C.CLAUDE_MAX_TURNS)]
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
            sources = [str(s) for s in d.get("sources", [])][:10]
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if mid in ids and 0.01 <= p <= 0.99 and 0 <= c <= 1:
            valid[mid] = {"p": p, "conf": c, "reasoning": str(d.get("reasoning", ""))[:1000],
                          "sources": json.dumps(sources), "leaked": is_leaked(sources)}
    return valid


def is_leaked(sources):
    for s in sources:
        host = (urlparse(s).hostname or s).lower()
        if any(d in host for d in C.PRICE_LEAK_DOMAINS):
            return True
    return False


# --- executable prices ---------------------------------------------------------------

def fee_per_share(price, fee_rate):
    return fee_rate * price * (1 - price)


def book(token_id):
    """Ask side of the CLOB book, best first, plus market constraints."""
    r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
    r.raise_for_status()
    b = r.json()
    asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", [])), key=lambda x: x[0])
    try:
        has_fee = float(requests.get(f"{CLOB}/fee-rate", params={"token_id": token_id},
                                     timeout=20).json().get("base_fee") or 0) > 0
    except (requests.RequestException, ValueError):
        has_fee = True
    return {"asks": asks, "min_size": float(b.get("min_order_size") or 5),
            "fee_rate": C.TAKER_FEE_RATE if has_fee else 0.0}


def all_in_cost(price, bk):
    return price + fee_per_share(price, bk["fee_rate"])


def walk_book(bk, stake):
    """Buy `stake` USD (fees included) against the asks.
    Returns (shares, avg all-in cost, worst price, fee USD) or None."""
    left, shares, fee, worst = stake, 0.0, 0.0, None
    for price, size in bk["asks"]:
        c = all_in_cost(price, bk)
        take = min(left, size * c)
        shares += take / c
        fee += take / c * (c - price)
        left -= take
        worst = price
        if left <= 1e-9:
            return shares, stake / shares, worst, fee
    return None  # not enough depth


def best_bet(p, yes_bk, no_bk):
    """Pick the side with the largest edge vs the all-in cost of the best ask.
    Returns (side, p_side, book, full-Kelly fraction) or None."""
    best = None
    for side, p_side, bk in (("YES", p, yes_bk), ("NO", 1 - p, no_bk)):
        if not bk["asks"]:
            continue
        c = all_in_cost(bk["asks"][0][0], bk)
        edge = p_side - c
        if edge >= C.MIN_EDGE and c < 1 and (best is None or edge > best[0]):
            best = (edge, side, p_side, bk, edge / (1 - c))
    return best[1:] if best else None


# --- risk --------------------------------------------------------------------------------

def risk_state(con, marks):
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    realized = one("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE status='closed' AND mode=?", C.MODE)
    unrealized = 0.0
    for mid, side, stake, shares in con.execute(
            "SELECT market_id, side, stake, shares FROM positions WHERE status='open' AND mode=?", (C.MODE,)):
        y = marks.get(mid)
        if y is not None:
            unrealized += shares * (y if side == "YES" else 1 - y) - stake
    return {
        "bankroll": C.BANKROLL_USD + realized,
        "equity": C.BANKROLL_USD + realized + unrealized,
        "exposure": one("SELECT COALESCE(SUM(stake),0) FROM positions WHERE status='open' AND mode=?", C.MODE),
        "today": one("SELECT COALESCE(SUM(stake),0) FROM positions WHERE ts >= ? AND mode=?", midnight, C.MODE),
        "n_open": one("SELECT COUNT(*) FROM positions WHERE status='open' AND mode=?", C.MODE),
    }


def place_live(token_id, stake, worst_price):
    """Fill-or-kill market buy capped at worst_price. Returns (order_id, usd_spent, shares) or raises.
    UNTESTED against a real account: verify on a $5 order before trusting it."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    client = ClobClient(CLOB, key=os.environ["POLY_PK"], chain_id=137,
                        signature_type=int(os.getenv("POLY_SIG_TYPE", "1")),
                        funder=os.environ["POLY_FUNDER"])
    client.set_api_creds(client.create_or_derive_api_creds())
    order = client.create_market_order(MarketOrderArgs(token_id=token_id, amount=round(stake, 2),
                                                       side=BUY, price=worst_price))
    resp = client.post_order(order, OrderType.FOK)
    if not resp or not resp.get("success") or resp.get("status") != "matched":
        raise RuntimeError(f"order not filled: {resp}")
    spent = float(resp.get("makingAmount") or stake)
    shares = float(resp.get("takingAmount") or 0) or stake / worst_price
    return resp.get("orderID"), spent, shares


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run()
    except Exception as e:
        log.exception("run crashed")
        send(f"❌ Run crashed: <code>{esc(type(e).__name__)}: {esc(str(e)[:400])}</code>")
        raise


def run():
    if C.KILL_SWITCH.exists():
        log.warning("KILL switch present, exiting")
        return
    con = db()
    marks = resolve_all(con)

    st = risk_state(con, marks)
    log.info("[%s] bankroll=%.2f equity=%.2f exposure=%.2f open=%d",
             C.MODE, st["bankroll"], st["equity"], st["exposure"], st["n_open"])
    if st["equity"] < C.BANKROLL_USD * (1 - C.MAX_DRAWDOWN):
        C.KILL_SWITCH.write_text(f"drawdown stop {now().isoformat()} equity={st['equity']:.2f}\n")
        log.error("Max drawdown hit, KILL created")
        send(f"🛑 <b>Drawdown stop</b>: equity ${st['equity']:.2f} of ${C.BANKROLL_USD:.0f}. "
             f"KILL created, agent stopped. Investigate before <code>rm KILL</code>.")
        return

    markets = fetch_candidates(con)
    log.info("candidates: %d", len(markets))
    if not markets:
        return
    try:
        preds = ask_claude(markets)
    except Exception as e:  # limits, timeout, bad output -> skip this run
        log.error("claude failed: %s", e)
        send(f"⚠️ Claude call failed, run skipped:\n<code>{esc(str(e)[:500])}</code>")
        return

    ts = now().isoformat()
    # Size off the smaller of realized bankroll and marked equity, so open losses shrink new bets.
    base = min(st["bankroll"], st["equity"])
    for m in markets:
        pr = preds.get(m["id"])
        if not pr:
            continue
        con.execute("INSERT INTO predictions(ts,market_id,question,p_model,p_market,confidence,reasoning,sources,leaked)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (ts, m["id"], m["question"], pr["p"], m["yes_price"], pr["conf"], pr["reasoning"],
                     pr["sources"], int(pr["leaked"])))
        con.commit()
        if pr["leaked"]:
            log.info("skip %s: forecast cited a price source", m["id"])
            continue
        if pr["conf"] < C.MIN_CONFIDENCE or abs(pr["p"] - m["yes_price"]) < C.MIN_EDGE:
            continue  # no chance of an edge; don't spend book requests
        if st["n_open"] >= C.MAX_OPEN_POSITIONS:
            break
        try:
            yes_bk, no_bk = book(m["yes_token"]), book(m["no_token"])
        except (requests.RequestException, ValueError) as e:
            log.warning("book failed %s: %s", m["id"], e)
            continue
        bet = best_bet(pr["p"], yes_bk, no_bk)
        if not bet:
            continue
        side, p_side, bk, f = bet
        stake = min(base * C.KELLY_FRACTION * f, base * C.MAX_POSITION_FRAC)
        stake = min(stake, base * C.MAX_TOTAL_EXPOSURE_FRAC - st["exposure"],
                    base * C.MAX_NEW_STAKE_PER_DAY_FRAC - st["today"])
        if stake < C.MIN_STAKE_USD:
            continue
        fill = walk_book(bk, stake)
        if not fill:
            continue
        shares, avg_cost, worst, fee = fill
        if p_side - avg_cost < C.MIN_EDGE or shares < bk["min_size"]:
            continue
        token = m["yes_token"] if side == "YES" else m["no_token"]
        order_id = None
        if C.MODE == "live":
            try:
                order_id, stake, shares = place_live(token, stake, worst)
            except Exception as e:
                log.error("order failed %s: %s", m["id"], e)
                continue
        con.execute("INSERT INTO positions(ts,market_id,side,token_id,price,stake,shares,mode,fee,order_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ts, m["id"], side, token, stake / shares, stake, shares, C.MODE, fee, order_id))
        con.commit()  # a live fill must never be lost to a later crash
        st["exposure"] += stake
        st["today"] += stake
        st["n_open"] += 1
        log.info("[%s] %s %s @%.3f (all-in) $%.2f | p=%.2f conf=%.2f | %s",
                 C.MODE, side, m["id"], stake / shares, stake, pr["p"], pr["conf"], m["question"][:80])
        send(f"🟢 <b>New {side}</b> ${stake:.2f} @ {stake / shares:.3f} (all-in, fee ${fee:.2f})\n"
             f"<b>{esc(m['question'])}</b>\n"
             f"model P(YES)={pr['p']:.2f} vs market {m['yes_price']:.2f} | conf {pr['conf']:.2f}\n"
             f"<i>{esc(pr['reasoning'][:600])}</i>\n"
             f"exposure ${st['exposure']:.2f}, open {st['n_open']}", url=m["url"])


if __name__ == "__main__":
    main()
