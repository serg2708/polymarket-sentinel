#!/usr/bin/env python3
"""Live liquidity-rewards market maker for Polymarket. DRY-RUN BY DEFAULT.

Same strategy the shadow MM measures (mm/shadow_mm.py): two-sided maker-only quotes at mid ± SPREAD
on a few reward markets, expressed as BUY YES at mid-s and BUY NO at (1-mid)-s so no inventory is
needed to quote. Safety, in order of importance:

  * exchange heartbeat — Polymarket cancels ALL our orders if a heartbeat is missed for 10 s, so a
    crash, network loss or power cut never leaves quotes on the book
  * our orders cancelled on start, on exit (SIGTERM/SIGINT) and when the KILL file exists (only on
    the bot's markets; the exchange heartbeat, however, cancels EVERY order on the account)
  * daily loss limit on marked equity -> cancel-all + KILL + Telegram
  * per-market inventory cap (stop bidding the side we already hold too much of)
  * quotes pulled STOP_BEFORE_EVENT before the event, outside [0.10, 0.90], < 1 h before end
  * post_only orders: we never take liquidity, never pay taker fees

    python mm/live_mm.py --auto 2              # dry run on the 2 best markets from the shadow MM
    MM_LIVE=1 python mm/live_mm.py --live --markets <condition_id>,<condition_id>

Live needs POLY_PK, POLY_FUNDER (and POLY_SIG_TYPE, used by the heartbeat client) in the environment
(~/.config/polysentinel/secrets.env via the systemd unit).
"""
import argparse
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shadow_mm as SH  # noqa: E402
from notify import esc, send  # noqa: E402

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "live.db"
KILL = HERE / "KILL"
CLOB = SH.CLOB

CAPITAL_PER_MARKET = float(os.getenv("MM_CAPITAL_PER_MARKET", "50"))
MAX_DAILY_LOSS = float(os.getenv("MM_MAX_DAILY_LOSS", "15"))
SPREAD = float(os.getenv("MM_SPREAD", "0.02"))   # live quotes 2c from mid: fewer fills than the shadow's 1c
MAX_INV_FRAC = 1.0               # net inventory cap per market = this × quote size
MIN_ORDER = 5                    # exchange minimum order size (shares)
CASH_BUFFER = 0.98               # never commit the last 2% of cash (rounding, fees)
LOOP_S = 15
HEARTBEAT_S = 4
END_BUFFER_S = 3600
FILL_COOLDOWN_S = 300            # after any fill on a market: no new buys there for 5 min
JUMP = 0.03                      # mid moved more than this within JUMP_WINDOW_S ...
JUMP_WINDOW_S = 90
JUMP_PAUSE_S = 300               # ... -> no buys on that market for 5 min
PENDING_S = 60                   # our fresh orders may not show in the open-orders listing yet

log = logging.getLogger("live_mm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity(ts REAL, day TEXT, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS fills(ts INTEGER, trade_id TEXT PRIMARY KEY, cond TEXT, token TEXT, side TEXT,
                                 price REAL, size REAL);
CREATE TABLE IF NOT EXISTS scoring(ts REAL, orders INTEGER, scoring INTEGER);
"""


# --- exchange adapters ---------------------------------------------------------------------------

class DryRunExchange:
    """Logs what would be sent; keeps an in-memory order book of our own orders; never fills."""

    def __init__(self, cash=100.0):
        self.orders, self.n, self._cash, self.bal, self.hidden = {}, 0, cash, {}, set()

    def cash(self):
        return self._cash

    def balance(self, token):
        return self.bal.get(token, 0.0)

    def open_orders(self, cond):
        return [o for o in self.orders.values() if o["cond"] == cond and o["id"] not in self.hidden]

    def place(self, cond, token, side, price, size, tick, neg_risk):
        self.n += 1
        oid = f"dry{self.n}"
        self.orders[oid] = {"id": oid, "cond": cond, "token": token, "side": side, "price": price, "size": size}
        log.info("[dry] %s %s @ %.3f x %.1f", side, token[-6:], price, size)
        return oid

    def cancel(self, ids):
        for i in ids:
            self.orders.pop(i, None)
        if ids:
            log.info("[dry] cancel %d", len(ids))

    def cancel_all(self):
        self.cancel(list(self.orders))

    def cancel_market(self, cond):
        self.cancel([i for i, o in self.orders.items() if o["cond"] == cond])

    def heartbeat(self):
        pass

    def fills_since(self, ts):
        return []

    def earnings(self, day):
        return None

    def scoring(self, ids):
        return {}


class LiveExchange:
    """Polymarket's unified SDK (polymarket-client) for orders and account data.

    The legacy py-clob-client signs orders in a format the exchange now rejects ("invalid order
    version"), but its heartbeat endpoint is the only one exposed: it is used for that alone.
    Verified 2026-09-26 on the owner's account: post-only order accepted, and auto-cancelled by the
    exchange ~10 s after heartbeats stopped.
    """

    def __init__(self):
        from polymarket import SecureClient
        from py_clob_client.client import ClobClient
        self.funder = os.environ["POLY_FUNDER"]
        self.sc = SecureClient.create(private_key=os.environ["POLY_PK"], wallet=self.funder)
        self.hb_client = ClobClient(CLOB, key=os.environ["POLY_PK"], chain_id=137,
                                    signature_type=int(os.getenv("POLY_SIG_TYPE", "2")), funder=self.funder)
        self.hb_client.set_api_creds(self.hb_client.create_or_derive_api_creds())
        self.hb_id = None

    def cash(self):
        return int(self.sc.get_balance_allowance(asset_type="COLLATERAL").balance) / 1e6

    def balance(self, token):
        return int(self.sc.get_balance_allowance(asset_type="CONDITIONAL", token_id=token).balance) / 1e6

    def open_orders(self, cond):
        return [{"id": o.id, "cond": cond, "token": o.asset_id, "side": str(o.side).upper(), "price": float(o.price),
                 "size": float(o.original_size) - float(o.size_matched)}
                for o in self.sc.list_open_orders(market=cond).iter_items()]

    def place(self, cond, token, side, price, size, tick, neg_risk):
        # tick size and neg-risk are resolved by the SDK itself
        r = self.sc.place_limit_order(token_id=token, price=str(price), size=str(size), side=side, post_only=True)
        if not r.ok:
            raise RuntimeError(f"order rejected: {getattr(r, 'code', '')} {getattr(r, 'message', '')}")
        log.info("placed %s %s @ %.3f x %.1f", side, token[-6:], price, size)
        return r.order_id

    def cancel(self, ids):
        if ids:
            self.sc.cancel_orders(order_ids=list(ids))
            log.info("cancelled %d order(s)", len(ids))

    def cancel_market(self, cond):
        self.sc.cancel_market_orders(market=cond)

    def cancel_all(self):
        self.sc.cancel_all()

    def heartbeat(self):
        r = self.hb_client.post_heartbeat(self.hb_id)
        self.hb_id = (r or {}).get("heartbeat_id", self.hb_id)

    def fills_since(self, ts):
        """Our own part of each trade: a trade record carries the taker's side and total size; our
        maker order is in maker_orders."""
        out = []
        for t in self.sc.list_account_trades(maker_address=self.funder, after=str(int(ts))).iter_items():
            d = t.model_dump()
            when = int(d["matched_at"].timestamp()) if d.get("matched_at") else None
            for i, mo in enumerate(d.get("maker_orders") or []):
                if str(mo.get("maker_address", "")).lower() != self.funder.lower():
                    continue
                out.append({"id": f"{d['id']}:{i}", "market": d.get("market") or d.get("condition_id"),
                            "asset_id": mo.get("asset_id"), "outcome": mo.get("outcome"),
                            "side": str(mo.get("side")).upper(), "price": float(mo.get("price") or 0),
                            "size": float(mo.get("matched_amount") or 0), "match_time": when})
        return out

    def scoring(self, ids):
        return self.sc.get_orders_scoring(order_ids=list(ids)) if ids else {}

    def earnings(self, day):
        """Liquidity rewards the exchange has accrued to us for a UTC day (paid out after midnight)."""
        return float(sum(e.earnings for e in self.sc.get_total_earnings_for_user_for_day(date=day)))


# --- strategy (pure, unit-tested) -------------------------------------------------------------------

def targets(m, bids, asks, inv_yes, inv_no, now, cash=float("inf"), capital=CAPITAL_PER_MARKET, spread=SPREAD,
            allow_buys=True):
    """Desired resting orders {(token, side): (price, size)} for one market, or {} with a reason.

    Each side of the quote is expressed the cheapest way we can afford:
      bid (we get long YES):  SELL the NO we hold at 1-bid, else BUY YES at bid
      ask (we get long NO):   SELL the YES we hold at ask,  else BUY NO at 1-ask
    Selling inventory needs no cash (and scores for rewards the same); buys are sized to cash.
    """
    mid = SH.adjusted_mid(bids, asks, m["min_size"])
    if mid is None:
        return {}, "no book"
    if m.get("event_ts") and now >= m["event_ts"] - SH.STOP_BEFORE_EVENT:
        return {}, "event imminent"
    if now >= m["end_ts"] - END_BUFFER_S:
        return {}, "market ending"
    if not SH.MID_RANGE[0] <= mid <= SH.MID_RANGE[1]:
        return {}, "mid outside 10-90c"
    bid, ask = SH.our_quote(mid, m["tick"], spread)
    # Inventory is never offered below the book's raw midpoint: the size-cutoff mid used for reward
    # scoring can sit far from it in a thin book (pilot 1: 0.61 vs 0.69) and would dump the position.
    raw_mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2 if bids and asks else mid
    tick = m["tick"]
    # Buys never cross the book's raw midpoint either: a few large orders can drag the size-cutoff
    # mid far away (Gemini: reward mid ~0.50 while the book was 0.35/0.39 -> our bid at 0.49 was hit).
    bid = min(bid, round(math.floor(round((raw_mid - spread) / tick, 6)) * tick, 4))
    ask = max(ask, round(math.ceil(round((raw_mid + spread) / tick, 6)) * tick, 4))
    if bid <= 0 or ask >= 1:
        return {}, "no room to quote"
    sell_yes_px = round(max(ask, math.ceil(round(raw_mid / tick, 6)) * tick), 4)
    sell_no_px = round(max(1 - bid, math.ceil(round((1 - raw_mid) / tick, 6)) * tick), 4)
    size = round(max(capital / (1 - 2 * spread), m["min_size"]), 2)
    cap, net = MAX_INV_FRAC * size, inv_yes - inv_no
    avail = cash * CASH_BUFFER
    out = {}
    if inv_no >= MIN_ORDER:
        out[(m["no"], "SELL")] = (sell_no_px, floor2(min(inv_no, size)))
    elif net < cap and allow_buys:
        sz = floor2(min(size, cap - net, avail / bid))           # never past the inventory cap
        if sz >= MIN_ORDER:
            out[(m["yes"], "BUY")] = (bid, sz)
            avail -= sz * bid
    if inv_yes >= MIN_ORDER:
        out[(m["yes"], "SELL")] = (sell_yes_px, floor2(min(inv_yes, size)))
    elif -net < cap and allow_buys:
        p = round(1 - ask, 4)
        sz = floor2(min(size, cap + net, avail / p))
        if sz >= MIN_ORDER:
            out[(m["no"], "BUY")] = (p, sz)
    return out, None if out else ("buys paused" if not allow_buys else "no affordable side")


def jumped(history, now, mid, jump=JUMP, window=JUMP_WINDOW_S):
    """True if the raw mid moved more than `jump` against any sample from the last `window` s."""
    return any(now - t <= window and abs(mid - old) > jump for t, old in history)


def merge_pending(current, pending, now, ttl=PENDING_S):
    """Open orders as the exchange lists them, plus our own fresh orders it does not list yet.
    Pilot 2: an order placed a moment earlier was missing from the listing, the bot placed a
    second one, and both were filled (91.8 NO instead of 45.9)."""
    seen = {o["id"] for o in current}
    return list(current) + [o for o in pending.values() if now - o["ts"] < ttl and o["id"] not in seen]


def floor2(x):
    return math.floor(x * 100) / 100


def diff_orders(current, wanted, tick):
    """Orders to cancel and to place. An existing order at the right price and roughly the right
    size is kept, so it keeps its queue priority."""
    cancel, place, kept = [], [], set()
    for o in current:
        key = (o["token"], o.get("side", "BUY"))
        w = wanted.get(key)
        if w and abs(o["price"] - w[0]) < tick / 2 and key not in kept and 0.5 * w[1] <= o["size"] <= 1.05 * w[1]:
            kept.add(key)
        else:
            cancel.append(o["id"])
    for (token, side), (price, size) in wanted.items():
        if (token, side) not in kept:
            place.append((token, side, price, size))
    return cancel, place


# --- runner ----------------------------------------------------------------------------------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def market_info(cond):
    m = SH.get(f"{CLOB}/markets/{cond}")
    toks = m["tokens"]
    r = m.get("rewards") or {}
    return {"cond": cond, "question": m["question"], "yes": toks[0]["token_id"], "no": toks[1]["token_id"],
            "min_size": float(r.get("min_size") or 5), "v": float(r.get("max_spread") or 3),
            "tick": float(m.get("minimum_tick_size") or 0.01), "neg_risk": bool(m.get("neg_risk")),
            "end_ts": datetime.fromisoformat(m["end_date_iso"].replace("Z", "+00:00")).timestamp(),
            "event_ts": SH.event_start(m), "accepting": m.get("accepting_orders", True)}


# Markets fed by data that is published continuously or daily (streaming charts, views, weather,
# sports, asset prices, box office): informed traders pick off resting quotes the moment it lands.
# Pilot 1 was filled at 0.82 on a Spotify #2-song market minutes before it dropped to ~0.69.
INFO_RISK = re.compile(
    r"song|chart|spotify|billboard|streams?\b|netflix|youtube|views|video|temperature|precipitation|rain|snow|"
    r"box office|opening weekend|\bvs\.?\b|win on|o/u|spread|game|match|price of|bitcoin|btc|ethereum|eth\b|"
    r"solana|xrp|crypto|stock|s&p|nasdaq|dow jones|\(high\)|\(low\)|\bhit \$|reach \$|dip to|"
    r"\bmlb\b|\bnba\b|\bnfl\b|\bnhl\b|premier league|la liga|serie a|champions league|season|"
    r"pitcher|goals?\b|touchdowns?|home runs?|mvp|playoffs?", re.I)


def best_from_shadow(n, min_days=1):
    """Markets with the best measured net result per $ in the shadow MM (still tradeable)."""
    con = sqlite3.connect(SH.DB_PATH)
    rows = SH.summary(con)
    days = con.execute("SELECT COUNT(DISTINCT day) FROM epochs").fetchone()[0]
    if days < min_days:
        raise SystemExit(f"shadow MM has {days} day(s) of data; need {min_days}")
    q2cond = dict(con.execute("SELECT question, cond FROM markets WHERE tracking=1"))
    ranked = sorted((r for r in rows if r["q"] in q2cond and r["net"] > 0 and not INFO_RISK.search(r["q"])),
                    key=lambda r: -r["net"] / r["capital"])
    return [q2cond[r["q"]] for r in ranked[:n]]


class Runner:
    def __init__(self, ex, conds, live):
        self.ex, self.live, self.con = ex, live, db()
        self.markets = [market_info(c) for c in conds]
        self.stop = threading.Event()
        self.last_fill_ts = time.time()
        self.day, self.day_start_equity = None, None
        self.pending = {}                                # order id -> our fresh order (see merge_pending)
        self.prev_inv, self.mids, self.pause_until = {}, {}, {}

    # equity = cash + every held share marked to its token's mid
    def equity(self):
        cash, eq = self.ex.cash(), 0.0
        for m in self.markets:
            try:
                bids, asks = SH.book(m["yes"])
                mid = SH.adjusted_mid(bids, asks, m["min_size"]) or 0.5
            except requests.RequestException:
                mid = 0.5
            eq += self.ex.balance(m["yes"]) * mid + self.ex.balance(m["no"]) * (1 - mid)
        return cash + eq, cash

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:
                self.ex.heartbeat()
            except Exception as e:                   # a missed beat only means the exchange cancels all
                log.warning("heartbeat failed: %s", e)
            self.stop.wait(HEARTBEAT_S)

    def notify(self, text):
        send(text, tag=f"PolySentinel market maker [{'LIVE' if self.live else 'dry-run'}]")

    def cancel_own(self):
        """Cancel our orders only — the account may also hold the owner's manual orders."""
        for m in self.markets:
            self.ex.cancel_market(m["cond"])

    def kill(self, why):
        log.error("KILL: %s", why)
        try:
            self.cancel_own()
        finally:
            KILL.write_text(f"{datetime.now(timezone.utc).isoformat()} {why}\n")
            self.notify(f"🛑 <b>stopped</b>: {esc(why)}. All orders cancelled. Remove <code>mm/KILL</code> to restart.")
            self.stop.set()

    def check_risk(self):
        eq, cash = self.equity()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            if self.day is not None:
                try:
                    rew = self.ex.earnings(self.day)
                except Exception as e:
                    log.warning("earnings: %s", e)
                    rew = None
                self.notify(f"🧮 <b>day {self.day}</b>\n"
                            f"rewards accrued (exchange): {'n/a' if rew is None else f'${rew:.2f}'}\n"
                            f"equity ${self.day_start_equity:.2f} → ${eq:.2f} ({eq - self.day_start_equity:+.2f}; "
                            f"the day's rewards land in cash shortly after 00:00 UTC)\n{self.scoring_line()}")
            self.day, self.day_start_equity = day, eq
        self.con.execute("INSERT INTO equity VALUES(?,?,?,?)", (time.time(), day, eq, cash))
        self.con.commit()
        if eq < self.day_start_equity - MAX_DAILY_LOSS:
            self.kill(f"daily loss {eq - self.day_start_equity:+.2f} exceeds -{MAX_DAILY_LOSS:.0f}")

    def scoring_line(self):
        r = self.con.execute("SELECT SUM(orders), SUM(scoring) FROM scoring WHERE ts > ?",
                             (time.time() - 86400,)).fetchone()
        return f"orders scoring for rewards: {r[1] / r[0]:.0%}" if r and r[0] else "scoring: no data"

    def record_fills(self):
        new = self.ex.fills_since(self.last_fill_ts)
        for t in new:
            self.con.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?)",
                             (int(t.get("match_time") or t.get("timestamp") or time.time()), t.get("id"),
                              t.get("market"), t.get("asset_id"), t.get("side"), float(t.get("price") or 0),
                              float(t.get("size") or 0)))
        if new:
            self.last_fill_ts = time.time()
            self.con.commit()
            self.notify(f"💱 {len(new)} fill(s): " + ", ".join(
                f"{t.get('side')} {float(t.get('size') or 0):.1f} {t.get('outcome') or ''} @ {float(t.get('price') or 0):.2f}"
                for t in new[:5]))

    def quote_market(self, m, now):
        cond = m["cond"]
        bids, asks = SH.book(m["yes"])
        inv_yes, inv_no = self.ex.balance(m["yes"]), self.ex.balance(m["no"])
        # a fill on this market -> no new buys here for a while (don't get run over twice)
        prev = self.prev_inv.get(cond)
        if prev is not None and (abs(inv_yes - prev[0]) > 1e-6 or abs(inv_no - prev[1]) > 1e-6):
            self.pause_until[cond] = max(self.pause_until.get(cond, 0), now + FILL_COOLDOWN_S)
            log.info("fill on %s -> buys paused %d s", m["question"][:40], FILL_COOLDOWN_S)
        self.prev_inv[cond] = (inv_yes, inv_no)
        # a sudden move of the raw mid -> no new buys either (news or someone moving the book)
        if bids and asks:
            raw_mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
            hist = self.mids.setdefault(cond, [])
            if jumped(hist, now, raw_mid):
                self.pause_until[cond] = max(self.pause_until.get(cond, 0), now + JUMP_PAUSE_S)
                log.info("price jump on %s -> buys paused %d s", m["question"][:40], JUMP_PAUSE_S)
            hist.append((now, raw_mid))
            self.mids[cond] = [(t, x) for t, x in hist if now - t <= JUMP_WINDOW_S]
        allow = now >= self.pause_until.get(cond, 0)
        wanted, why = targets(m, bids, asks, inv_yes, inv_no, now, cash=self.ex.cash(), allow_buys=allow)
        if not m["accepting"]:
            wanted, why = {}, "not accepting orders"
        current = merge_pending(self.ex.open_orders(cond), {k: v for k, v in self.pending.items() if v["cond"] == cond}, now)
        cancel, place = diff_orders(current, wanted, m["tick"])
        self.ex.cancel(cancel)
        for oid in cancel:
            self.pending.pop(oid, None)
        ids = []
        for token, side, price, size in place:
            try:
                oid = self.ex.place(cond, token, side, price, size, m["tick"], m["neg_risk"])
                ids.append(oid)
                self.pending[oid] = {"id": oid, "cond": cond, "token": token, "side": side, "price": price,
                                     "size": size, "ts": now}
            except Exception as e:
                log.warning("place failed %s: %s", m["question"][:40], e)
        for oid in [k for k, v in self.pending.items() if now - v["ts"] >= PENDING_S]:
            del self.pending[oid]
        if why:
            log.info("flat %s: %s", m["question"][:50], why)
        return ids

    def run(self):
        if KILL.exists():
            raise SystemExit(f"{KILL} exists — remove it to start")
        self.cancel_own()                             # clean slate on our markets
        signal.signal(signal.SIGTERM, lambda *a: self.stop.set())
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        self.notify("▶️ started: " +
             "; ".join(esc(m["question"][:50]) for m in self.markets))
        last_scoring = 0.0
        try:
            while not self.stop.is_set():
                if KILL.exists():
                    self.kill("KILL file")
                    break
                self.check_risk()
                if self.stop.is_set():
                    break
                now = time.time()
                for m in self.markets:
                    try:
                        self.quote_market(m, now)
                    except requests.RequestException as e:
                        log.warning("quote %s: %s", m["question"][:40], e)
                self.record_fills()
                if now - last_scoring > 600:
                    ids = [o["id"] for m in self.markets for o in self.ex.open_orders(m["cond"])]
                    res = self.ex.scoring(ids) or {}
                    self.con.execute("INSERT INTO scoring VALUES(?,?,?)",
                                     (now, len(ids), sum(1 for v in res.values() if v)))
                    self.con.commit()
                    last_scoring = now
                self.stop.wait(LOOP_S)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            try:
                self.cancel_own()
            finally:
                log.info("stopped, our orders cancelled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", help="comma-separated condition ids")
    ap.add_argument("--auto", type=int, help="take the N best markets from the shadow MM")
    ap.add_argument("--live", action="store_true", help="send real orders (also needs MM_LIVE=1)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per HTTP call drowns the log
    conds = a.markets.split(",") if a.markets else best_from_shadow(a.auto or 2)
    if not conds:
        raise SystemExit("no markets: shadow MM has no market with a positive net result yet")
    live = a.live and os.getenv("MM_LIVE") == "1"
    if a.live and not live:
        raise SystemExit("--live also needs MM_LIVE=1 in the environment")
    Runner(LiveExchange() if live else DryRunExchange(), conds, live).run()


if __name__ == "__main__":
    main()
