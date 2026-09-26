#!/usr/bin/env python3
"""Is Polymarket systematically mis-calibrated anywhere we could trade (e.g. favourite-longshot bias)?

Takes resolved binary markets, samples the YES price weekly while each market was open (calendar
times, so markets that resolved early are not dropped = no survivorship bias), buckets by price and
compares the realized YES rate. Then simulates buying YES or NO in each bucket after spread + fee.

    python quant/calibration_study.py [--markets 1500] [--since 2026-03-01]
"""
import argparse
import json
import math
import random
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from touch_backtest import CLOB, FEE_RATE, GAMMA, HALF_SPREAD, cached, get

DAY = 86400
BUCKETS = [0.01, 0.03, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.97, 0.99]


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def closed_markets(n, since, until):
    out, off = [], 0
    while len(out) < n:
        try:
            page = cached(f"closed_{since}_{until}_{off}.json", lambda o=off: get(
                f"{GAMMA}/markets", closed="true", limit=100, offset=o, order="volumeNum", ascending="false",
                end_date_min=f"{since}T00:00:00Z", end_date_max=f"{until}T00:00:00Z"))
        except RuntimeError:
            break                                    # gamma caps list offsets (~2000)
        if not page:
            break
        for m in page:
            try:
                p = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                outs = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
            except ValueError:
                continue
            if outs == ["yes", "no"] and len(p) == 2 and max(p) >= 0.99 and m.get("clobTokenIds"):
                out.append(m)
        off += 100
    return out[:n]


def price_at(token, t):
    h = cached(f"pt_{token}_{int(t)}.json", lambda: get(
        f"{CLOB}/prices-history", market=token, startTs=int(t - 6 * 3600), endTs=int(t), fidelity=60
    ).get("history", []))
    return float(max(h, key=lambda x: x["t"])["p"]) if h else None


def samples_for(m):
    start, end = ts(m.get("startDate") or m.get("createdAt")), ts(m.get("endDate"))
    closed = ts(m.get("closedTime")) or end
    if not start or not end:
        return []
    stop = min(closed, end) - DAY
    token = json.loads(m["clobTokenIds"])[0]
    y = 1 if float(json.loads(m["outcomePrices"])[0]) > 0.5 else 0
    out, t = [], start + DAY
    t = math.ceil(t / (7 * DAY)) * 7 * DAY                     # fixed weekly calendar grid
    while t < stop:
        p = price_at(token, t)
        if p is not None and 0.005 < p < 0.995:
            out.append({"m": m["id"], "t": t, "p": p, "y": y, "days_left": (end - t) / DAY})
        t += 7 * DAY
    return out


def cost(price):
    c = price + HALF_SPREAD
    return c + FEE_RATE * c * (1 - c)


def roi(rows, side):
    """Mean return per $1 for buying `side`, with a market-clustered bootstrap 90% interval."""
    per = {}
    for r in rows:
        c = cost(r["p"] if side == "YES" else 1 - r["p"])
        if not 0 < c < 1:
            continue
        win = r["y"] == (1 if side == "YES" else 0)
        per.setdefault(r["m"], []).append((1 / c if win else 0) - 1)
    vals = [statistics.mean(v) for v in per.values()]
    if len(vals) < 5:
        return None
    rnd = random.Random(0)
    boots = sorted(statistics.mean(rnd.choices(vals, k=len(vals))) for _ in range(1000))
    return statistics.mean(vals), boots[50], boots[949], len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=1500)
    ap.add_argument("--since", default="2026-03-01")
    ap.add_argument("--until", default="2026-09-20")
    a = ap.parse_args()
    ms = closed_markets(a.markets, a.since, a.until)
    with ThreadPoolExecutor(8) as ex:
        rows = [r for rs in ex.map(samples_for, ms) for r in rs]
    print(f"{len(ms)} resolved markets, {len(rows)} weekly price samples "
          f"({len({r['m'] for r in rows})} markets with samples)\n")
    print(f"{'price bucket':14} {'n':>5} {'mkts':>5} {'avg price':>9} {'YES rate':>8}   "
          f"{'buy YES ROI (90% CI)':>24}   {'buy NO ROI (90% CI)':>24}")
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        b = [r for r in rows if lo <= r["p"] < hi]
        if not b:
            continue
        cells = []
        for side in ("YES", "NO"):
            res = roi(b, side)
            cells.append(f"{res[0]:+6.1%} [{res[1]:+5.1%},{res[2]:+5.1%}]" if res else "-")
        print(f"{lo:.2f}-{hi:.2f}     {len(b):5} {len({r['m'] for r in b}):5} "
              f"{statistics.mean(r['p'] for r in b):9.3f} {statistics.mean(r['y'] for r in b):8.3f}   "
              f"{cells[0]:>24}   {cells[1]:>24}")
    json.dump(rows, open(__import__("pathlib").Path(__file__).parent / "cache" / "calib_rows.json", "w"))


if __name__ == "__main__":
    main()
