#!/usr/bin/env python3
"""Market-making P&L on resolved reward markets, BEFORE rewards: does adverse selection eat the pool?

For each resolved market that had liquidity rewards, replay its real trades (data-api) against our
two-sided quote of N shares at mid ± s, where mid is the market price one minute earlier (we requote
each minute, so we are always a minute stale). A taker selling YES at or below our bid fills us, a
taker buying YES at or above our ask fills us. Inventory is settled at the resolved outcome.

Optimistic on queue priority (we are assumed first at our price), so fills and adverse selection are
if anything overstated. Output: trading P&L per market-day next to the market's reward pool per day.

    python quant/mm_backtest.py [--markets 60] [--bankroll 300] [--spread 1.0]
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from touch_backtest import CLOB, GAMMA, cached, get

DATA = "https://data-api.polymarket.com"
DAY = 86400


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def rewarded_closed(n, days=21):
    """Resolved markets that had a reward pool, from day-by-day windows (gamma caps list offsets)."""
    from datetime import timedelta, timezone
    out, seen = [], set()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, days + 1):
        lo, hi = (today - timedelta(days=d)).isoformat(), (today - timedelta(days=d - 1)).isoformat()
        for off in range(0, 500, 100):
            try:
                page = cached(f"closedday_{lo[:10]}_{off}.json", lambda o=off, lo=lo, hi=hi: get(
                    f"{GAMMA}/markets", closed="true", limit=100, offset=o, order="volumeNum",
                    ascending="false", end_date_min=lo, end_date_max=hi))
            except RuntimeError:
                break
            for m in page:
                rate = sum(float(r.get("rewardsDailyRate") or 0) for r in m.get("clobRewards") or [])
                try:
                    p = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                except ValueError:
                    continue
                if rate > 0 and len(p) == 2 and max(p) >= 0.99 and m.get("conditionId") and m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(dict(m, rate=rate))
            if len(page) < 100:
                break
    # spread the sample across kinds instead of taking the first n
    out.sort(key=lambda m: hash(m["id"]) % 1000)
    return out[:n]


def trades(cond):
    def fetch():
        out, off = [], 0
        while off <= 3000:
            page = get(f"{DATA}/trades", market=cond, limit=500, offset=off)
            if not page:
                break
            out += page
            off += 500
        return out
    return cached(f"trades_{cond}.json", fetch)


def minute_prices(token, start, end):
    def fetch():
        out, t = [], int(start)
        while t < end:
            out += get(f"{CLOB}/prices-history", market=token, startTs=t, endTs=min(int(end), t + DAY),
                       fidelity=1).get("history", [])
            t += DAY
        return out
    return sorted({(int(x["t"]), float(x["p"])) for x in cached(f"min_{token}_{int(start)}.json", fetch)})


EVENT_RE = __import__("re").compile(
    r"on (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), "
    r"(\d{4}),? at (\d{1,2}):(\d{2}) ?(AM|PM) ET")


def event_start(m):
    """When the thing the market is about starts (speech, game, call) — quotes must be gone by then."""
    for k in ("gameStartTime", "eventStartTime"):
        if m.get(k):
            try:
                return ts(m[k].replace(" ", "T") if "T" not in m[k] else m[k])
            except ValueError:
                pass
    g = EVENT_RE.search(m.get("description") or "")
    if not g:
        return None
    mon, day, year, hh, mm, ap = g.groups()
    h = int(hh) % 12 + (12 if ap == "PM" else 0)
    local = datetime.strptime(f"{year} {mon} {day} {h}:{mm}", "%Y %B %d %H:%M")
    return (local - datetime(1970, 1, 1)).total_seconds() + 4 * 3600      # ET = UTC-4 (EDT)


def simulate(m, bankroll, s, stop_before_event=None):
    start = ts(m.get("startDate") or m["createdAt"])
    end = min(ts(m["closedTime"]) if m.get("closedTime") else ts(m["endDate"]), ts(m["endDate"]) + DAY)
    start = max(start, end - 7 * DAY)                        # at most the last week of the market
    y = 1 if float(json.loads(m["outcomePrices"])[0]) > 0.5 else 0
    token = json.loads(m["clobTokenIds"])[0]
    px = minute_prices(token, start, end)
    tr = sorted((t for t in trades(m["conditionId"]) if start <= t["timestamp"] <= end), key=lambda t: t["timestamp"])
    if len(px) < 30:
        return None
    n = bankroll / (1 - 2 * s)                               # shares per side our bankroll supports
    ev = event_start(m) if stop_before_event is not None else None
    if stop_before_event is not None and ev is None and " say " in m["question"].lower():
        return None                                          # can't place the event in time: skip
    cash, inv, filled, i = 0.0, 0.0, 0.0, 0
    for t in tr:
        while i + 1 < len(px) and px[i + 1][0] <= t["timestamp"] - 60:
            i += 1
        if ev is not None and t["timestamp"] >= ev - stop_before_event:
            break                                            # quotes pulled before the event starts
        mid = px[i][1]
        if not 0.10 <= mid <= 0.90 or px[i][0] > t["timestamp"] - 60:
            continue                                         # we only quote the two-sided-scoring range
        yes_px = t["price"] if t["outcome"] == "Yes" else 1 - t["price"]
        sells_yes = (t["side"] == "SELL") == (t["outcome"] == "Yes")
        if sells_yes and yes_px <= mid - s:                  # taker sells YES into our bid
            size = min(float(t["size"]), n - inv)            # bankroll caps our long inventory
            if size <= 0:
                continue
            cash -= (mid - s) * size
            inv += size
            filled += size
        elif not sells_yes and yes_px >= mid + s:            # taker buys YES from our ask
            size = min(float(t["size"]), n + inv)            # ...and our short (= long NO) inventory
            if size <= 0:
                continue
            cash += (mid + s) * size
            inv -= size
            filled += size
    pnl = cash + inv * y
    days = max((end - start) / DAY, 1 / 24)
    q = m["question"].lower()
    kind = ("mention" if " say " in q or "mention" in q else "weather" if "temperature" in q or "precipitation" in q
            else "sports" if any(k in q for k in (" vs", "o/u", "spread:", "win on", "game")) else "other")
    return {"q": m["question"], "kind": kind, "rate": m["rate"], "days": days, "pnl": pnl, "pnl_day": pnl / days,
            "filled": filled, "trades": len(tr)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=60)
    ap.add_argument("--bankroll", type=float, default=300)
    ap.add_argument("--spread", type=float, default=1.0, help="cents from mid")
    ap.add_argument("--stop-before-event", type=float, default=None,
                    help="pull quotes this many minutes before the event starts")
    a = ap.parse_args()
    ms = rewarded_closed(a.markets)

    def safe(m):
        try:
            return simulate(m, a.bankroll, a.spread / 100,
                            None if a.stop_before_event is None else a.stop_before_event * 60)
        except (RuntimeError, KeyError, ValueError):
            return None
    with ThreadPoolExecutor(6) as ex:
        rows = [r for r in ex.map(safe, ms) if r]
    print(f"{len(rows)} resolved reward markets simulated, ${a.bankroll:.0f} two-sided at ±{a.spread}c\n")
    print(f"{'trade P&L/day':>13} {'pool/day':>8} {'filled sh':>9} {'trades':>6}  market")
    for r in sorted(rows, key=lambda r: r["pnl_day"])[:12]:
        print(f"{r['pnl_day']:13.2f} {r['rate']:8.0f} {r['filled']:9.0f} {r['trades']:6}  {r['q'][:62]}")
    print()
    for kind in ("mention", "weather", "sports", "other"):
        k = [r for r in rows if r["kind"] == kind]
        if k:
            d = sum(r["days"] for r in k)
            print(f"{kind:8} markets {len(k):3}  trading P&L/day {sum(r['pnl'] for r in k) / d:+7.2f}  "
                  f"pool/day avg {sum(r['rate'] for r in k) / len(k):6.0f}  losing {sum(r['pnl'] < 0 for r in k)}/{len(k)}  "
                  f"worst {min(r['pnl'] for r in k):+.0f}")
    tot_days = sum(r["days"] for r in rows)
    print(f"\ntotal trading P&L {sum(r['pnl'] for r in rows):+.2f} over {tot_days:.1f} market-days "
          f"= {sum(r['pnl'] for r in rows) / tot_days:+.2f}/day per market (rewards NOT included)")
    losers = [r for r in rows if r["pnl"] < 0]
    print(f"markets with a trading loss: {len(losers)} of {len(rows)}; "
          f"worst {min(r['pnl'] for r in rows):+.2f}")


if __name__ == "__main__":
    main()
