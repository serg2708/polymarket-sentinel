#!/usr/bin/env python3
"""How much could a small bankroll earn from Polymarket liquidity rewards right now?

For every reward-eligible market (CLOB /sampling-markets): read both order books, score the
competing resting liquidity with Polymarket's formula (docs: programs/liquidity-rewards), and
estimate our daily share if we quote N shares two-sided (bid YES at mid-s, bid NO at 1-mid-s).

    python quant/rewards_scan.py [--bankroll 300] [--min-rate 10] [--spread 1.0]

This is a snapshot estimate: competitors' depth changes during the day, fills create inventory
(adverse selection is NOT modelled) and payouts under $1/day are not paid.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from touch_backtest import CLOB, get

C_SINGLE = 3.0   # single-sided divisor (docs: c = 3.0)


def reward_markets(min_rate):
    out, cursor = [], ""
    while cursor != "LTE=":
        d = get(f"{CLOB}/sampling-markets", next_cursor=cursor) if cursor else get(f"{CLOB}/sampling-markets")
        for m in d.get("data", []):
            r = m.get("rewards") or {}
            rate = sum(float(x.get("rewards_daily_rate") or 0) for x in r.get("rates") or [])
            toks = m.get("tokens") or []
            end = m.get("end_date_iso")
            live = end and datetime.fromisoformat(end.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(hours=6)
            if rate >= min_rate and len(toks) == 2 and m.get("accepting_orders") and not m.get("closed") and live:
                out.append({"q": m["question"], "slug": m.get("market_slug"), "rate": rate,
                            "min_size": float(r.get("min_size") or 0), "v": float(r.get("max_spread") or 0),
                            "yes": toks[0]["token_id"], "no": toks[1]["token_id"],
                            "tick": float(m.get("minimum_tick_size") or 0.01), "end": m.get("end_date_iso")})
        cursor = d.get("next_cursor") or "LTE="
    return out


def book(token):
    b = get(f"{CLOB}/book", token_id=token)
    return ([(float(x["price"]), float(x["size"])) for x in b.get("bids", [])],
            [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])


def score(v, spread_c):
    return ((v - spread_c) / v) ** 2 if 0 <= spread_c < v else 0.0


def analyse(m, bankroll, s_c):
    # The NO book is the mirror of the YES book (a YES bid at p shows as a NO ask at 1-p),
    # so reading both would double-count every order.
    try:
        yb, ya = book(m["yes"])
    except RuntimeError:
        return None
    if not yb or not ya:
        return None
    bid, ask = max(p for p, _ in yb), min(p for p, _ in ya)
    if ask - bid > 2 * m["v"] / 100:
        return None                                  # no real two-sided market to quote around
    mid = (bid + ask) / 2
    v = m["v"]
    qual = lambda size: size >= m["min_size"]
    # book one: YES bids (= NO asks); book two: YES asks (= NO bids)  (docs eq. 2-3)
    q_one = sum(score(v, (mid - p) * 100) * sz for p, sz in yb if qual(sz))
    q_two = sum(score(v, (p - mid) * 100) * sz for p, sz in ya if qual(sz))
    two_sided_only = mid < 0.10 or mid > 0.90
    others = min(q_one, q_two) if two_sided_only else max(min(q_one, q_two), max(q_one, q_two) / C_SINGLE)
    # our quote: N shares bid on YES at mid - s and N on NO at (1 - mid) - s
    s = s_c / 100
    unit_cost = (mid - s) + ((1 - mid) - s)
    n = bankroll / unit_cost if unit_cost > 0 else 0
    if n < m["min_size"] or s_c >= v:
        return dict(m, mid=mid, others=others, ours=0, share=0, per_day=0, note="bankroll < min size")
    ours = score(v, s_c) * n
    share = ours / (ours + others) if ours + others else 1
    per_day = m["rate"] * share
    return dict(m, mid=mid, others=others, ours=ours, share=share, per_day=per_day,
                note="" if per_day >= 1 else "< $1/day is not paid")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=300)
    ap.add_argument("--min-rate", type=float, default=10)
    ap.add_argument("--spread", type=float, default=1.0, help="our distance from mid, cents")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    ms = reward_markets(a.min_rate)
    total_pool = sum(m["rate"] for m in ms)
    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(lambda m: analyse(m, a.bankroll, a.spread), ms) if r]
    rows.sort(key=lambda r: -r["per_day"])
    print(f"{len(ms)} reward markets with >= ${a.min_rate:.0f}/day, total pool ${total_pool:,.0f}/day; "
          f"bankroll ${a.bankroll:.0f} in ONE market, quoting {a.spread}c from mid both sides\n")
    print(f"{'$/day':>6} {'APR':>6} {'share':>6} {'pool':>6} {'mid':>5} {'v':>4} {'min':>5}  market")
    for r in rows[:a.top]:
        apr = r["per_day"] * 365 / a.bankroll
        print(f"{r['per_day']:6.2f} {apr:6.0%} {r['share']:6.1%} {r['rate']:6.0f} {r['mid']:5.2f} "
              f"{r['v']:4.1f} {r['min_size']:5.0f}  {r['q'][:60]} {r['note']}")
    paid = [r for r in rows if r["per_day"] >= 1]
    print(f"\nmarkets where this bankroll would clear the $1/day payout minimum: {len(paid)}")


if __name__ == "__main__":
    main()
