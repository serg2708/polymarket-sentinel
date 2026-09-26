#!/usr/bin/env python3
"""Shadow market maker: measures what liquidity-rewards market making WOULD earn, with no money.

Every minute, for the tracked reward markets, it reads the live YES book, places a virtual
two-sided quote (N shares at mid ± SPREAD), scores it against the competing book with Polymarket's
formula (docs: programs/liquidity-rewards) and replays the market's real trades against it.

  reward estimate  = daily_rate × Σ(our share per minute) / 1440      (one sample per minute)
  trading P&L      = fills against real takers, inventory marked to mid, settled at resolution

Risk rules mirror what a live bot would do: quotes pulled STOP_BEFORE_EVENT before the event the
market is about (game start / speech time), no quoting outside [0.10, 0.90], inventory capped by
the capital allotted to the market.

    python mm/shadow_mm.py            # run forever (systemd: polysentinel-mm.service)
    python mm/shadow_mm.py --report   # print results so far
"""
import argparse
import json
import logging
import math
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notify import esc, send  # noqa: E402

CLOB, GAMMA, DATA = "https://clob.polymarket.com", "https://gamma-api.polymarket.com", "https://data-api.polymarket.com"
DB_PATH = Path(__file__).resolve().parent / "mm.db"

CAPITAL_PER_MARKET = 100.0       # virtual $ per market; results are reported per $ too
SPREAD = 0.01                    # quote distance from mid
N_MARKETS = 20
MAX_PER_GROUP = 2                # sibling brackets of one event move together: diversify
STOP_BEFORE_EVENT = 5 * 60       # s
MIN_RATE = 20.0                  # $/day pool to consider
MID_RANGE = (0.10, 0.90)
C_SINGLE = 3.0                   # docs: single-sided score divisor
SAMPLES_PER_DAY = 1440
EXCLUDE = re.compile(r"up or down|\d{1,2}:\d{2}\s?(am|pm)\s?-\s?\d", re.I)   # 5-min/15-min crypto candles
EVENT_RE = re.compile(
    r"on (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), "
    r"(\d{4}),? at (\d{1,2}):(\d{2}) ?(AM|PM) ET")

log = logging.getLogger("shadow_mm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  cond TEXT PRIMARY KEY, question TEXT, slug TEXT, yes TEXT, rate REAL, min_size REAL, v REAL, tick REAL,
  end_ts REAL, event_ts REAL, capital REAL, shares REAL, tracking INTEGER DEFAULT 1,
  cash REAL DEFAULT 0, inv REAL DEFAULT 0, last_mid REAL, last_trade_ts INTEGER DEFAULT 0,
  bid REAL, ask REAL, quote_ts INTEGER, settled INTEGER DEFAULT 0, outcome REAL, added_ts REAL);
CREATE TABLE IF NOT EXISTS epochs(
  cond TEXT, day TEXT, rate REAL, share_sum REAL DEFAULT 0, samples INTEGER DEFAULT 0, quoted INTEGER DEFAULT 0,
  PRIMARY KEY(cond, day));
CREATE TABLE IF NOT EXISTS fills(
  ts INTEGER, cond TEXT, side TEXT, price REAL, size REAL, mid_after REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def get(url, **params):
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# --- pure pieces (unit-tested) ---------------------------------------------------------------

def score(v_cents, spread_cents):
    """Polymarket order score S(v, s) = ((v - s) / v)^2 inside the max spread, else 0."""
    return ((v_cents - spread_cents) / v_cents) ** 2 if 0 <= spread_cents < v_cents else 0.0


def adjusted_mid(bids, asks, min_size):
    """Midpoint of the best levels that meet the size cutoff (falls back to the raw touch)."""
    qb = [p for p, s in bids if s >= min_size] or [p for p, _ in bids]
    qa = [p for p, s in asks if s >= min_size] or [p for p, _ in asks]
    if not qb or not qa:
        return None
    return (max(qb) + min(qa)) / 2


def book_q(bids, asks, mid, v, min_size):
    """Competitors' (Q_one, Q_two) from the YES book (the NO book is its mirror)."""
    q1 = sum(score(v, (mid - p) * 100) * s for p, s in bids if s >= min_size)
    q2 = sum(score(v, (p - mid) * 100) * s for p, s in asks if s >= min_size)
    return q1, q2


def q_min(q1, q2, mid):
    if mid < 0.10 or mid > 0.90:
        return min(q1, q2)
    return max(min(q1, q2), max(q1, q2) / C_SINGLE)


def our_quote(mid, tick, spread=SPREAD):
    bid = math.floor(round((mid - spread) / tick, 6)) * tick
    ask = math.ceil(round((mid + spread) / tick, 6)) * tick
    return round(bid, 4), round(ask, 4)


def sample_share(bids, asks, mid, v, min_size, bid, ask, shares):
    """Our share of this minute's reward sample if our quote were resting in this book."""
    o1, o2 = book_q(bids, asks, mid, v, min_size)
    ours1 = score(v, (mid - bid) * 100) * shares
    ours2 = score(v, (ask - mid) * 100) * shares
    ours = q_min(ours1, ours2, mid)
    others = q_min(o1, o2, mid)
    return ours / (ours + others) if ours > 0 else 0.0


def apply_trade(t, bid, ask, inv, shares):
    """Fill from one real trade against our quote. Returns (side, price, size) or None.
    A taker selling YES at/below our bid hits us; a taker buying YES at/above our ask lifts us."""
    yes_px = t["price"] if t["outcome"] == "Yes" else 1 - t["price"]
    sells_yes = (t["side"] == "SELL") == (t["outcome"] == "Yes")
    if sells_yes and yes_px <= bid + 1e-9:
        size = min(float(t["size"]), shares - inv)
        return ("BUY", bid, size) if size > 0 else None
    if not sells_yes and yes_px >= ask - 1e-9:
        size = min(float(t["size"]), shares + inv)
        return ("SELL", ask, size) if size > 0 else None
    return None


def group_key(question):
    """Sibling markets of one event share their opening words and differ in numbers/brackets
    ("MrBeast's next video ... 35-40M in week 1" vs "... 20-22.5M on day 1")."""
    q = re.sub(r"[\d.,$%°]+[a-zA-Z]?", "#", question.lower())
    return " ".join(q.split()[:6])


def event_start(m):
    for k in ("game_start_time", "gameStartTime"):
        if m.get(k):
            try:
                return datetime.fromisoformat(str(m[k]).replace("Z", "+00:00").replace(" ", "T")).timestamp()
            except ValueError:
                pass
    g = EVENT_RE.search(m.get("description") or "")
    if not g:
        return None
    mon, day, year, hh, mm, ap = g.groups()
    local = datetime.strptime(f"{year} {mon} {day} {int(hh) % 12 + (12 if ap == 'PM' else 0)}:{mm}",
                              "%Y %B %d %H:%M")
    return (local - datetime(1970, 1, 1)).total_seconds() + 4 * 3600          # ET = UTC-4 (EDT)


# --- market selection --------------------------------------------------------------------------

def book(token):
    b = get(f"{CLOB}/book", token_id=token)
    return ([(float(x["price"]), float(x["size"])) for x in b.get("bids", [])],
            [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])


def candidates():
    out, cursor, now = [], "", time.time()
    while cursor != "LTE=":
        d = get(f"{CLOB}/sampling-markets", **({"next_cursor": cursor} if cursor else {}))
        for m in d.get("data", []):
            r = m.get("rewards") or {}
            rate = sum(float(x.get("rewards_daily_rate") or 0) for x in r.get("rates") or [])
            toks = m.get("tokens") or []
            end = m.get("end_date_iso")
            if (rate < MIN_RATE or len(toks) != 2 or not end or not m.get("accepting_orders") or m.get("closed")
                    or EXCLUDE.search(m.get("question", ""))):
                continue
            end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            ev = event_start(m)
            if end_ts < now + 6 * 3600 or (ev and ev < now + 3600):
                continue
            if not ev and re.search(r"\bsay\b|mention", m.get("question", ""), re.I):
                continue                              # a speech market we can't place in time: too risky
            out.append({"cond": m["condition_id"], "question": m["question"], "slug": m.get("market_slug"),
                        "yes": toks[0]["token_id"], "rate": rate, "min_size": float(r.get("min_size") or 0),
                        "v": float(r.get("max_spread") or 0), "tick": float(m.get("minimum_tick_size") or 0.01),
                        "end_ts": end_ts, "event_ts": ev})
        cursor = d.get("next_cursor") or "LTE="
    return out


def expected_per_day(c):
    try:
        bids, asks = book(c["yes"])
    except requests.RequestException:
        return None
    mid = adjusted_mid(bids, asks, c["min_size"])
    if mid is None or not MID_RANGE[0] <= mid <= MID_RANGE[1] or c["v"] <= SPREAD * 100:
        return None
    if min(p for p, _ in asks) - max(p for p, _ in bids) > 2 * c["v"] / 100:
        return None
    capital = max(CAPITAL_PER_MARKET, c["min_size"] * (1 - 2 * SPREAD) * 1.02)
    shares = capital / (1 - 2 * SPREAD)
    bid, ask = our_quote(mid, c["tick"])
    share = sample_share(bids, asks, mid, c["v"], c["min_size"], bid, ask, shares)
    return dict(c, capital=capital, shares=shares, est=c["rate"] * share, est_per_100=c["rate"] * share * 100 / capital)


def reselect(con):
    cands = sorted(candidates(), key=lambda c: -c["rate"])[:200]
    with ThreadPoolExecutor(8) as ex:
        scored = [x for x in ex.map(expected_per_day, cands) if x and x["est"] > 0]
    scored.sort(key=lambda x: -x["est_per_100"])
    tracked = con.execute("SELECT cond, question FROM markets WHERE tracking=1").fetchall()
    keep = {c for c, _ in tracked}
    groups = {}
    for _, q in tracked:
        groups[group_key(q)] = groups.get(group_key(q), 0) + 1
    new = []
    for x in scored:
        g = group_key(x["question"])
        if len(keep) + len(new) >= N_MARKETS:
            break
        if x["cond"] in keep or groups.get(g, 0) >= MAX_PER_GROUP:
            continue
        groups[g] = groups.get(g, 0) + 1
        new.append(x)
    for x in new:
        con.execute("INSERT OR IGNORE INTO markets(cond,question,slug,yes,rate,min_size,v,tick,end_ts,event_ts,"
                    "capital,shares,last_trade_ts,added_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (x["cond"], x["question"], x["slug"], x["yes"], x["rate"], x["min_size"], x["v"], x["tick"],
                     x["end_ts"], x["event_ts"], x["capital"], x["shares"], int(time.time()), time.time()))
    con.commit()
    log.info("reselect: %d candidates scored, %d added, %d tracked", len(scored), len(new), len(keep) + len(new))


# --- the minute loop ----------------------------------------------------------------------------

def market_trades(cond, since):
    page = get(f"{DATA}/trades", market=cond, limit=500)
    return sorted((t for t in page if t["timestamp"] > since), key=lambda t: t["timestamp"])


def step(con, row, now):
    (cond, yes, rate, min_size, v, tick, end_ts, event_ts, shares, cash, inv, last_ts, bid, ask, quote_ts) = row
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    # 1. fills from real trades against the quote that was resting since the last step
    if bid is not None and quote_ts:
        for t in market_trades(cond, max(last_ts, quote_ts)):
            f = apply_trade(t, bid, ask, inv, shares)
            last_ts = max(last_ts, t["timestamp"])
            if f:
                side, px, size = f
                cash += -px * size if side == "BUY" else px * size
                inv += size if side == "BUY" else -size
                con.execute("INSERT INTO fills VALUES(?,?,?,?,?,NULL)", (t["timestamp"], cond, side, px, size))
    else:
        last_ts = max(last_ts, int(now))
    # 2. new quote + reward sample
    bids, asks = book(yes)
    mid = adjusted_mid(bids, asks, min_size)
    stop = (event_ts and now >= event_ts - STOP_BEFORE_EVENT) or now >= end_ts
    quoting = mid is not None and not stop and MID_RANGE[0] <= mid <= MID_RANGE[1]
    share = 0.0
    if quoting:
        bid, ask = our_quote(mid, tick)
        share = sample_share(bids, asks, mid, v, min_size, bid, ask, shares)
    else:
        bid = ask = None
    con.execute("INSERT OR IGNORE INTO epochs(cond, day, rate) VALUES(?,?,?)", (cond, day, rate))
    con.execute("UPDATE epochs SET share_sum=share_sum+?, samples=samples+1, quoted=quoted+? WHERE cond=? AND day=?",
                (share, int(quoting), cond, day))
    con.execute("UPDATE markets SET cash=?, inv=?, last_trade_ts=?, bid=?, ask=?, quote_ts=?, last_mid=COALESCE(?, last_mid),"
                " tracking=? WHERE cond=?",
                (cash, inv, last_ts, bid, ask, int(now) if quoting else None, mid, int(not stop), cond))


def settle(con):
    """Resolve markets that stopped being quoted and are closed on Polymarket."""
    for cond, cash, inv in con.execute("SELECT cond, cash, inv FROM markets WHERE tracking=0 AND settled=0").fetchall():
        try:
            m = get(f"{CLOB}/markets/{cond}")
        except requests.RequestException:
            continue
        winners = [t for t in m.get("tokens", []) if t.get("winner")]
        if m.get("closed") and winners:
            y = 1.0 if winners[0].get("outcome") == "Yes" else 0.0
            con.execute("UPDATE markets SET settled=1, outcome=?, cash=?, inv=0 WHERE cond=?", (y, cash + inv * y, cond))
    con.commit()


def run():
    con = db()
    last_select = 0.0
    while True:
        started = time.time()
        try:
            if started - last_select > 3600:
                reselect(con)
                settle(con)
                last_select = started
            rows = con.execute("SELECT cond, yes, rate, min_size, v, tick, end_ts, event_ts, shares, cash, inv,"
                               " last_trade_ts, bid, ask, quote_ts FROM markets WHERE tracking=1").fetchall()
            for row in rows:
                try:
                    step(con, row, time.time())
                except (requests.RequestException, ValueError, KeyError) as e:
                    log.warning("step %s: %s", row[0][:10], e)
            con.commit()
            daily_report(con)
        except Exception:
            log.exception("loop error")
        time.sleep(max(5, 60 - (time.time() - started)))


# --- reporting ---------------------------------------------------------------------------------

def summary(con, day=None):
    where, args = ("WHERE e.day=?", (day,)) if day else ("", ())
    rows = con.execute(
        "SELECT m.question, m.capital, m.cash, m.inv, m.last_mid, m.settled, "
        f"SUM(e.rate * e.share_sum / {SAMPLES_PER_DAY}), SUM(e.samples), SUM(e.quoted) "
        f"FROM markets m JOIN epochs e ON e.cond=m.cond {where} GROUP BY m.cond", args).fetchall()
    out = []
    for q, cap, cash, inv, mid, settled, reward, samples, quoted in rows:
        trading = cash + (0 if settled else inv * (mid or 0))
        out.append({"q": q, "capital": cap, "reward": reward or 0, "trading": trading, "net": (reward or 0) + trading,
                    "samples": samples, "quoted": quoted, "open_inv": 0 if settled else inv})
    return out


def text_report(con, day=None):
    rows = sorted(summary(con, day), key=lambda r: -r["net"])
    if not rows:
        return "no data yet"
    cap = sum(r["capital"] for r in rows)
    rew, trd = sum(r["reward"] for r in rows), sum(r["trading"] for r in rows)
    first = con.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM epochs").fetchone()
    lines = [f"{'Day ' + day if day else 'All time ' + str(first[0]) + '..' + str(first[1])}: "
             f"{len(rows)} markets, virtual capital ${cap:,.0f}",
             f"rewards est  ${rew:+8.2f}", f"trading P&L  ${trd:+8.2f}  (open inventory marked to mid)",
             f"NET          ${rew + trd:+8.2f}  = {100 * (rew + trd) / cap:+.2f}% of capital", ""]
    for r in rows[:6] + (rows[-3:] if len(rows) > 9 else []):
        lines.append(f"{r['net']:+7.2f} (rew {r['reward']:6.2f}, trd {r['trading']:+7.2f}) {r['q'][:48]}")
    return "\n".join(lines)


def daily_report(con):
    today = datetime.now(timezone.utc)
    yday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if today.hour == 0 and today.minute < 10:
        return
    done = con.execute("SELECT v FROM meta WHERE k='reported'").fetchone()
    if done and done[0] >= yday:
        return
    if con.execute("SELECT COUNT(*) FROM epochs WHERE day=?", (yday,)).fetchone()[0]:
        send(f"🧮 <b>Shadow market maker — {yday}</b> (no real money)\n<pre>{esc(text_report(con, yday))}</pre>")
    con.execute("INSERT OR REPLACE INTO meta VALUES('reported', ?)", (yday,))
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--day")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.report:
        print(text_report(db(), a.day))
        return
    run()


if __name__ == "__main__":
    main()
