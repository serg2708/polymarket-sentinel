#!/usr/bin/env python3
"""Live liquidity-rewards market maker for Polymarket. DRY-RUN BY DEFAULT.

Same strategy the shadow MM measures (mm/shadow_mm.py): two-sided maker-only quotes at mid ± SPREAD
on a few reward markets, expressed as BUY YES at mid-s and BUY NO at (1-mid)-s so no inventory is
needed to quote. Safety, in order of importance:

  * exchange heartbeat — Polymarket cancels ALL our orders if a heartbeat is missed for 10 s, so a
    crash, network loss or power cut never leaves quotes on the book
  * cancel-all on start, on exit (SIGTERM/SIGINT) and when the KILL file exists
  * daily loss limit on marked equity -> cancel-all + KILL + Telegram
  * per-market inventory cap (stop bidding the side we already hold too much of)
  * quotes pulled STOP_BEFORE_EVENT before the event, outside [0.10, 0.90], < 1 h before end
  * post_only orders: we never take liquidity, never pay taker fees

    python mm/live_mm.py --auto 2              # dry run on the 2 best markets from the shadow MM
    MM_LIVE=1 python mm/live_mm.py --live --markets <condition_id>,<condition_id>

Live needs POLY_PK, POLY_FUNDER (and POLY_SIG_TYPE, default 1) in the environment
(~/.config/polysentinel/secrets.env via the systemd unit).
"""
import argparse
import logging
import os
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
SPREAD = SH.SPREAD
MAX_INV_FRAC = 1.0               # net inventory cap per market = this × quote size
LOOP_S = 15
HEARTBEAT_S = 4
END_BUFFER_S = 3600

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
        self.orders, self.n, self._cash = {}, 0, cash

    def cash(self):
        return self._cash

    def balance(self, token):
        return 0.0

    def open_orders(self, cond):
        return [o for o in self.orders.values() if o["cond"] == cond]

    def place(self, cond, token, price, size, tick, neg_risk):
        self.n += 1
        oid = f"dry{self.n}"
        self.orders[oid] = {"id": oid, "cond": cond, "token": token, "price": price, "size": size}
        log.info("[dry] BUY %s @ %.3f x %.1f", token[-6:], price, size)
        return oid

    def cancel(self, ids):
        for i in ids:
            self.orders.pop(i, None)
        if ids:
            log.info("[dry] cancel %d", len(ids))

    def cancel_all(self):
        self.cancel(list(self.orders))

    def heartbeat(self):
        pass

    def fills_since(self, ts):
        return []

    def scoring(self, ids):
        return {}


class LiveExchange:
    def __init__(self):
        from py_clob_client.client import ClobClient
        self.client = ClobClient(CLOB, key=os.environ["POLY_PK"], chain_id=137,
                                 signature_type=int(os.getenv("POLY_SIG_TYPE", "1")),
                                 funder=os.environ["POLY_FUNDER"])
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.funder = os.environ["POLY_FUNDER"]
        self.hb_id = None

    def _bal(self, **kw):
        from py_clob_client.clob_types import BalanceAllowanceParams
        r = self.client.get_balance_allowance(BalanceAllowanceParams(**kw))
        return float(r.get("balance") or 0) / 1e6

    def cash(self):
        from py_clob_client.clob_types import AssetType
        return self._bal(asset_type=AssetType.COLLATERAL)

    def balance(self, token):
        from py_clob_client.clob_types import AssetType
        return self._bal(asset_type=AssetType.CONDITIONAL, token_id=token)

    def open_orders(self, cond):
        from py_clob_client.clob_types import OpenOrderParams
        out = []
        for o in self.client.get_orders(OpenOrderParams(market=cond)) or []:
            left = float(o.get("original_size") or 0) - float(o.get("size_matched") or 0)
            out.append({"id": o["id"], "cond": cond, "token": o["asset_id"], "price": float(o["price"]), "size": left})
        return out

    def place(self, cond, token, price, size, tick, neg_risk):
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY
        order = self.client.create_order(OrderArgs(token_id=token, price=price, size=size, side=BUY),
                                         PartialCreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk))
        r = self.client.post_order(order, OrderType.GTC, post_only=True)
        if not r or not r.get("success"):
            raise RuntimeError(f"order rejected: {r}")
        return r.get("orderID")

    def cancel(self, ids):
        if ids:
            self.client.cancel_orders(list(ids))

    def cancel_all(self):
        self.client.cancel_all()

    def heartbeat(self):
        r = self.client.post_heartbeat(self.hb_id)
        self.hb_id = (r or {}).get("heartbeat_id", self.hb_id)

    def fills_since(self, ts):
        from py_clob_client.clob_types import TradeParams
        return self.client.get_trades(TradeParams(maker_address=self.funder, after=int(ts))) or []

    def scoring(self, ids):
        from py_clob_client.clob_types import OrdersScoringParams
        return self.client.are_orders_scoring(OrdersScoringParams(orderIds=list(ids))) if ids else {}


# --- strategy (pure, unit-tested) -------------------------------------------------------------------

def targets(m, bids, asks, inv_yes, inv_no, now, capital=CAPITAL_PER_MARKET, spread=SPREAD):
    """Desired resting orders {token: (price, size)} for one market, or {} with a reason to be flat."""
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
    no_bid = round(1 - ask, 4)
    size = round(max(capital / (1 - 2 * spread), m["min_size"]), 2)
    net = inv_yes - inv_no
    out = {}
    if net < MAX_INV_FRAC * size:                 # not already long YES beyond the cap
        out[m["yes"]] = (bid, size)
    if -net < MAX_INV_FRAC * size:                # not already long NO beyond the cap
        out[m["no"]] = (no_bid, size)
    return out, None


def diff_orders(current, wanted, tick):
    """Orders to cancel and to place. An existing order at the right price and roughly the right
    size is kept, so it keeps its queue priority."""
    cancel, place, kept = [], [], set()
    for o in current:
        w = wanted.get(o["token"])
        if w and abs(o["price"] - w[0]) < tick / 2 and o["token"] not in kept and o["size"] >= 0.5 * w[1]:
            kept.add(o["token"])
        else:
            cancel.append(o["id"])
    for token, (price, size) in wanted.items():
        if token not in kept:
            place.append((token, price, size))
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


def best_from_shadow(n, min_days=1):
    """Markets with the best measured net result per $ in the shadow MM (still tradeable)."""
    con = sqlite3.connect(SH.DB_PATH)
    rows = SH.summary(con)
    days = con.execute("SELECT COUNT(DISTINCT day) FROM epochs").fetchone()[0]
    if days < min_days:
        raise SystemExit(f"shadow MM has {days} day(s) of data; need {min_days}")
    q2cond = dict(con.execute("SELECT question, cond FROM markets WHERE tracking=1"))
    ranked = sorted((r for r in rows if r["q"] in q2cond and r["net"] > 0), key=lambda r: -r["net"] / r["capital"])
    return [q2cond[r["q"]] for r in ranked[:n]]


class Runner:
    def __init__(self, ex, conds, live):
        self.ex, self.live, self.con = ex, live, db()
        self.markets = [market_info(c) for c in conds]
        self.stop = threading.Event()
        self.last_fill_ts = time.time()
        self.day, self.day_start_equity = None, None

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

    def kill(self, why):
        log.error("KILL: %s", why)
        try:
            self.ex.cancel_all()
        finally:
            KILL.write_text(f"{datetime.now(timezone.utc).isoformat()} {why}\n")
            send(f"🛑 <b>Live MM stopped</b>: {esc(why)}. All orders cancelled. Remove <code>mm/KILL</code> to restart.")
            self.stop.set()

    def check_risk(self):
        eq, cash = self.equity()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            if self.day is not None:
                send(f"🧮 <b>Live MM {'LIVE' if self.live else 'dry-run'} — {self.day}</b>\n"
                     f"equity ${self.day_start_equity:.2f} → ${eq:.2f} ({eq - self.day_start_equity:+.2f}, "
                     f"includes rewards paid at 00:00 UTC)\n{self.scoring_line()}")
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
            send(f"💱 Live MM: {len(new)} fill(s) — " + ", ".join(
                f"{t.get('side')} {float(t.get('size') or 0):.0f} @ {float(t.get('price') or 0):.2f}" for t in new[:5]))

    def quote_market(self, m, now):
        bids, asks = SH.book(m["yes"])
        wanted, why = targets(m, bids, asks, self.ex.balance(m["yes"]), self.ex.balance(m["no"]), now)
        if not m["accepting"]:
            wanted, why = {}, "not accepting orders"
        cancel, place = diff_orders(self.ex.open_orders(m["cond"]), wanted, m["tick"])
        self.ex.cancel(cancel)
        ids = []
        for token, price, size in place:
            try:
                ids.append(self.ex.place(m["cond"], token, price, size, m["tick"], m["neg_risk"]))
            except Exception as e:
                log.warning("place failed %s: %s", m["question"][:40], e)
        if why:
            log.info("flat %s: %s", m["question"][:50], why)
        return ids

    def run(self):
        if KILL.exists():
            raise SystemExit(f"{KILL} exists — remove it to start")
        self.ex.cancel_all()                          # clean slate
        signal.signal(signal.SIGTERM, lambda *a: self.stop.set())
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        send(f"▶️ Live MM started ({'LIVE' if self.live else 'dry-run'}): " +
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
                self.ex.cancel_all()
            finally:
                log.info("stopped, all orders cancelled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", help="comma-separated condition ids")
    ap.add_argument("--auto", type=int, help="take the N best markets from the shadow MM")
    ap.add_argument("--live", action="store_true", help="send real orders (also needs MM_LIVE=1)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conds = a.markets.split(",") if a.markets else best_from_shadow(a.auto or 2)
    if not conds:
        raise SystemExit("no markets: shadow MM has no market with a positive net result yet")
    live = a.live and os.getenv("MM_LIVE") == "1"
    if a.live and not live:
        raise SystemExit("--live also needs MM_LIVE=1 in the environment")
    Runner(LiveExchange() if live else DryRunExchange(), conds, live).run()


if __name__ == "__main__":
    main()
