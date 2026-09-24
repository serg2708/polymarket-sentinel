#!/usr/bin/env python3
"""Closes resolved markets and reports calibration: `python resolve.py`."""
import json

import requests

GAMMA = "https://gamma-api.polymarket.com"


def outcome_of(market_id):
    r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=20)
    if r.status_code != 200:
        return None
    m = r.json()
    if not m.get("closed"):
        return None
    raw = m.get("outcomePrices")
    prices = [float(x) for x in (json.loads(raw) if isinstance(raw, str) else raw or [])]
    if len(prices) == 2 and max(prices) >= 0.99:
        return 1.0 if prices[0] > prices[1] else 0.0
    return None  # closed but not cleanly resolved yet (dispute, 50/50) -> retry later


def resolve_all(con):
    ids = {x[0] for x in con.execute(
        "SELECT market_id FROM predictions WHERE outcome IS NULL "
        "UNION SELECT market_id FROM positions WHERE status='open'")}
    for mid in ids:
        y = outcome_of(mid)
        if y is None:
            continue
        con.execute("UPDATE predictions SET outcome=? WHERE market_id=? AND outcome IS NULL", (y, mid))
        rows = con.execute("SELECT id, side, stake, shares FROM positions "
                           "WHERE market_id=? AND status='open'", (mid,)).fetchall()
        for pid, side, stake, shares in rows:
            won = (side == "YES") == (y == 1.0)
            con.execute("UPDATE positions SET status='closed', pnl=? WHERE id=?",
                        ((shares if won else 0.0) - stake, pid))
    con.commit()


def report(con):
    rows = con.execute("SELECT p_model, p_market, outcome FROM predictions WHERE outcome IS NOT NULL").fetchall()
    n = len(rows)
    print(f"Resolved forecasts: {n}")
    if n:
        bm = sum((p - y) ** 2 for p, _, y in rows) / n
        bq = sum((q - y) ** 2 for _, q, y in rows) / n
        print(f"Brier model:  {bm:.4f}\nBrier market: {bq:.4f}  (lower is better)")
        if n < 30:
            print("Verdict: not enough data (<30)")
        else:
            print("Verdict:", "model beats market" if bm < bq else "NO EDGE, do not go live")
    for mode, cnt, pnl in con.execute("SELECT mode, COUNT(*), COALESCE(SUM(pnl),0) FROM positions "
                                      "WHERE status='closed' GROUP BY mode"):
        print(f"[{mode}] closed={cnt} pnl=${pnl:.2f}")
    for mode, cnt, exp in con.execute("SELECT mode, COUNT(*), COALESCE(SUM(stake),0) FROM positions "
                                      "WHERE status='open' GROUP BY mode"):
        print(f"[{mode}] open={cnt} exposure=${exp:.2f}")


if __name__ == "__main__":
    from run_agent import db
    c = db()
    resolve_all(c)
    report(c)
