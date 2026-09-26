#!/usr/bin/env python3
"""Closes resolved markets and reports calibration: `python resolve.py`."""
import json
import math

import requests

import config as C
from notify import esc, market_url, send

GAMMA = "https://gamma-api.polymarket.com"


def market_state(market_id):
    """Returns (outcome or None, current YES price or None, page url or None)."""
    try:
        # list endpoint, unlike /markets/{id}, includes events[] (needed for the page url).
        # It hides closed markets unless asked, so try open first, then closed.
        m = None
        for extra in ({}, {"closed": "true"}):
            r = requests.get(f"{GAMMA}/markets", params={"id": market_id, **extra}, timeout=20)
            if r.status_code == 200 and r.json():
                m = r.json()[0]
                break
    except (requests.RequestException, ValueError):
        m = None
    if not m:
        return None, None, None
    url = market_url(m)
    raw = m.get("outcomePrices")
    try:
        prices = [float(x) for x in (json.loads(raw) if isinstance(raw, str) else raw or [])]
    except (ValueError, TypeError):
        return None, None, url
    mark = prices[0] if len(prices) == 2 else None
    if m.get("closed") and len(prices) == 2 and max(prices) >= 0.99:
        return (1.0 if prices[0] > prices[1] else 0.0), mark, url
    return None, mark, url  # open, or closed but not cleanly resolved yet (dispute, 50/50) -> retry later


def resolve_all(con):
    """Settles resolved markets. Returns {market_id: YES mark} for positions still open."""
    open_pos = {x[0] for x in con.execute("SELECT market_id FROM positions WHERE status='open'")}
    ids = open_pos | {x[0] for x in con.execute("SELECT market_id FROM predictions WHERE outcome IS NULL")}
    marks = {}
    for mid in ids:
        y, mark, url = market_state(mid)
        if y is None:
            if mid in open_pos and mark is not None:
                marks[mid] = mark
            continue
        con.execute("UPDATE predictions SET outcome=? WHERE market_id=? AND outcome IS NULL", (y, mid))
        rows = con.execute("SELECT id, side, stake, shares, mode FROM positions "
                           "WHERE market_id=? AND status='open'", (mid,)).fetchall()
        for pid, side, stake, shares, mode in rows:
            won = (side == "YES") == (y == 1.0)
            pnl = (shares if won else 0.0) - stake
            con.execute("UPDATE positions SET status='closed', pnl=? WHERE id=?", (pnl, pid))
            q = con.execute("SELECT question FROM predictions WHERE market_id=? ORDER BY id DESC LIMIT 1",
                            (mid,)).fetchone()
            send(f"{'✅ WON' if won else '🔻 LOST'} [{mode}] {side} ${stake:.2f} → PnL <b>{pnl:+.2f}</b>\n"
                 f"{esc(q[0] if q else mid)} → resolved {'YES' if y == 1.0 else 'NO'}", url=url)
    con.commit()
    return marks


def brier_line(label, rows):
    """rows = [(p_model, p_market, outcome)]. Paired test: is the model's Brier lower than the market's?"""
    n = len(rows)
    if not n:
        print(f"{label}: n=0")
        return
    d = [(p - y) ** 2 - (q - y) ** 2 for p, q, y in rows]      # <0 = model better on that market
    bm = sum((p - y) ** 2 for p, _, y in rows) / n
    bq = sum((q - y) ** 2 for _, q, y in rows) / n
    mean = sum(d) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1)) if n > 1 else 0.0
    z = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    if n < 30:
        verdict = "not enough data (<30)"
    elif z < -2:
        verdict = "model beats market (z<-2)"
    elif z > 2:
        verdict = "market beats model: NO EDGE, do not go live"
    else:
        verdict = "no significant difference: NO EDGE proven, do not go live"
    print(f"{label}: n={n}  Brier model={bm:.4f}  market={bq:.4f}  z={z:+.2f}  -> {verdict}")


def report(con):
    # First forecast per market only (re-forecasts are correlated and would inflate n); leaked excluded.
    rows = con.execute(
        "SELECT p_model, p_market, outcome, confidence FROM predictions p "
        "WHERE outcome IS NOT NULL AND COALESCE(leaked,0)=0 AND version=? AND id = ("
        "  SELECT MIN(id) FROM predictions WHERE market_id=p.market_id AND COALESCE(leaked,0)=0 AND version=?)",
        (C.FORECAST_VERSION, C.FORECAST_VERSION)).fetchall()
    old = con.execute("SELECT COUNT(*) FROM predictions WHERE COALESCE(version,1) < ?",
                      (C.FORECAST_VERSION,)).fetchone()[0]
    leaked = con.execute("SELECT COUNT(*) FROM predictions WHERE leaked=1").fetchone()[0]
    print(f"Forecast version v{C.FORECAST_VERSION}. Resolved markets (first forecast, not leaked): {len(rows)}   "
          f"leaked overall: {leaked}   older versions ignored: {old}")
    brier_line("All         ", [r[:3] for r in rows])
    brier_line("Confident   ", [r[:3] for r in rows if r[3] >= C.MIN_CONFIDENCE])

    for mode, cnt, pnl, fee, staked in con.execute(
            "SELECT mode, COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(fee),0), COALESCE(SUM(stake),0) "
            "FROM positions WHERE status='closed' GROUP BY mode"):
        roi = pnl / staked if staked else 0
        print(f"[{mode}] closed={cnt} pnl=${pnl:.2f} (fees ${fee:.2f}) ROI={roi:+.1%}")
    for mode, cnt, exp in con.execute("SELECT mode, COUNT(*), COALESCE(SUM(stake),0) FROM positions "
                                      "WHERE status='open' GROUP BY mode"):
        print(f"[{mode}] open={cnt} exposure=${exp:.2f}")


def positions(con, marks):
    """All positions: open ones marked to the current price, closed ones with realized PnL."""
    rows = con.execute(
        "SELECT p.ts, p.market_id, p.side, p.price, p.stake, p.shares, p.status, p.pnl, p.mode, "
        "  (SELECT question FROM predictions q WHERE q.market_id=p.market_id ORDER BY id DESC LIMIT 1), "
        "  (SELECT p_model FROM predictions q WHERE q.market_id=p.market_id AND q.ts<=p.ts ORDER BY id DESC LIMIT 1) "
        "FROM positions p ORDER BY p.id").fetchall()
    if not rows:
        print("No positions yet.")
        return
    print(f"{'date':10} {'mode':5} {'side':4} {'price':>5} {'stake':>7} {'model':>5} {'now':>5} "
          f"{'PnL':>8}  {'status':6} question")
    total_open = total_closed = 0.0
    for ts, mid, side, price, stake, shares, status, pnl, mode, q, p_model in rows:
        y = marks.get(mid)
        now = (y if side == "YES" else 1 - y) if y is not None else None
        if status == "open":
            val = shares * now - stake if now is not None else 0.0
            total_open += val
            pnl_s, now_s = (f"~{val:+.2f}" if now is not None else "?"), (f"{now:.3f}" if now is not None else "?")
        else:
            total_closed += pnl or 0.0
            pnl_s, now_s = f"{pnl or 0:+.2f}", "-"
        p_side = (p_model if side == "YES" else 1 - p_model) if p_model is not None else None
        print(f"{ts[:10]} {mode:5} {side:4} {price:5.3f} ${stake:6.2f} "
              f"{(f'{p_side:.2f}' if p_side is not None else '?'):>5} {now_s:>5} {pnl_s:>8}  {status:6} {(q or mid)[:70]}")
    print(f"\nRealized PnL: ${total_closed:+.2f}   Unrealized (at current price): ~${total_open:+.2f}")
    print("price/model/now are for the side held; ~ = not locked in until the market resolves")


if __name__ == "__main__":
    import contextlib
    import io
    import sys

    from run_agent import db
    c = db()
    marks = resolve_all(c)
    if "--positions" in sys.argv:
        positions(c, marks)
        sys.exit()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(c)
    print(buf.getvalue(), end="")
    if "--notify" in sys.argv:
        send(f"📊 <b>Weekly report</b>\n<pre>{esc(buf.getvalue())}</pre>")
