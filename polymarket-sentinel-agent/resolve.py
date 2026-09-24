#!/usr/bin/env python3
"""Closes resolved markets and reports calibration: `python resolve.py`."""
import json
import math

import requests

GAMMA = "https://gamma-api.polymarket.com"


def market_state(market_id):
    """Returns (outcome or None, current YES price or None)."""
    try:
        r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=20)
    except requests.RequestException:
        return None, None
    if r.status_code != 200:
        return None, None
    m = r.json()
    raw = m.get("outcomePrices")
    try:
        prices = [float(x) for x in (json.loads(raw) if isinstance(raw, str) else raw or [])]
    except (ValueError, TypeError):
        return None, None
    mark = prices[0] if len(prices) == 2 else None
    if m.get("closed") and len(prices) == 2 and max(prices) >= 0.99:
        return (1.0 if prices[0] > prices[1] else 0.0), mark
    return None, mark  # open, or closed but not cleanly resolved yet (dispute, 50/50) -> retry later


def resolve_all(con):
    """Settles resolved markets. Returns {market_id: YES mark} for positions still open."""
    open_pos = {x[0] for x in con.execute("SELECT market_id FROM positions WHERE status='open'")}
    ids = open_pos | {x[0] for x in con.execute("SELECT market_id FROM predictions WHERE outcome IS NULL")}
    marks = {}
    for mid in ids:
        y, mark = market_state(mid)
        if y is None:
            if mid in open_pos and mark is not None:
                marks[mid] = mark
            continue
        con.execute("UPDATE predictions SET outcome=? WHERE market_id=? AND outcome IS NULL", (y, mid))
        rows = con.execute("SELECT id, side, stake, shares FROM positions "
                           "WHERE market_id=? AND status='open'", (mid,)).fetchall()
        for pid, side, stake, shares in rows:
            won = (side == "YES") == (y == 1.0)
            con.execute("UPDATE positions SET status='closed', pnl=? WHERE id=?",
                        ((shares if won else 0.0) - stake, pid))
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
        "WHERE outcome IS NOT NULL AND COALESCE(leaked,0)=0 AND id = ("
        "  SELECT MIN(id) FROM predictions WHERE market_id=p.market_id AND COALESCE(leaked,0)=0)").fetchall()
    leaked = con.execute("SELECT COUNT(*) FROM predictions WHERE leaked=1").fetchone()[0]
    print(f"Resolved markets (first forecast, not leaked): {len(rows)}   leaked forecasts overall: {leaked}")
    brier_line("All         ", [r[:3] for r in rows])
    import config as C
    brier_line("Confident   ", [r[:3] for r in rows if r[3] >= C.MIN_CONFIDENCE])

    for mode, cnt, pnl, fee, staked in con.execute(
            "SELECT mode, COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(fee),0), COALESCE(SUM(stake),0) "
            "FROM positions WHERE status='closed' GROUP BY mode"):
        roi = pnl / staked if staked else 0
        print(f"[{mode}] closed={cnt} pnl=${pnl:.2f} (fees ${fee:.2f}) ROI={roi:+.1%}")
    for mode, cnt, exp in con.execute("SELECT mode, COUNT(*), COALESCE(SUM(stake),0) FROM positions "
                                      "WHERE status='open' GROUP BY mode"):
        print(f"[{mode}] open={cnt} exposure=${exp:.2f}")


if __name__ == "__main__":
    from run_agent import db
    c = db()
    resolve_all(c)
    report(c)
