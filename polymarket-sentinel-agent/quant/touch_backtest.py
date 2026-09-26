#!/usr/bin/env python3
"""Backtest a volatility model on Polymarket crypto touch markets ("Will Bitcoin reach/dip to $X in <month>?").

For every resolved market and every day it traded, compare at 12:00 UTC:
  - model: P(price touches the strike before the deadline), driftless GBM barrier formula,
           sigma from the previous 30 days of hourly Binance returns (no look-ahead)
  - market: Polymarket YES price at that moment (CLOB price history)
against the resolved outcome, and simulate buying the side the model favours.

    python quant/touch_backtest.py [--months 12] [--assets bitcoin,ethereum]
"""
import argparse
import json
import math
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GAMMA, CLOB, BINANCE = "https://gamma-api.polymarket.com", "https://clob.polymarket.com", "https://api.binance.com"
CACHE = Path(__file__).resolve().parent / "cache"
SYMBOL = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT", "xrp": "XRPUSDT"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
HOUR = 3600
FEE_RATE = 0.07          # taker fee per share = rate * p * (1 - p)
HALF_SPREAD = 0.01       # we buy at mid + 1c


def cached(name, fn):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / name
    if f.exists():
        return json.loads(f.read_text())
    data = fn()
    f.write_text(json.dumps(data))
    return data


def get(url, **params):
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed: {url} {params}")


def hourly_closes(symbol, start, end):
    """{hour_ts: (close, high, low)} from Binance 1h klines."""
    def fetch():
        out, t = {}, int(start) * 1000
        while t < end * 1000:
            ks = get(f"{BINANCE}/api/v3/klines", symbol=symbol, interval="1h", startTime=t, limit=1000)
            if not ks:
                break
            for k in ks:
                out[str(k[0] // 1000)] = [float(k[4]), float(k[2]), float(k[3])]
            t = ks[-1][0] + HOUR * 1000
        return out
    return {int(k): v for k, v in cached(f"{symbol}_{int(start)}_{int(end)}.json", fetch).items()}


def events(asset, months):
    now = datetime.now(timezone.utc)
    out = []
    for back in range(1, months + 1):
        d = (now.replace(day=1) - timedelta(days=1)) if back == 1 else d.replace(day=1) - timedelta(days=1)
        for slug in (f"what-price-will-{asset}-hit-in-{MONTHS[d.month - 1]}-{d.year}",
                     f"what-price-will-{asset}-hit-in-{MONTHS[d.month - 1]}"):
            ev = cached(f"event_{slug}.json", lambda s=slug: get(f"{GAMMA}/events", slug=s))
            if ev and ev[0].get("markets"):
                out.append((d.year, d.month, ev[0]))
                break
    return out


STRIKE = re.compile(r"\b(reach|hit|dip to|drop to|fall to)\b[^$\d]*\$?([\d,]+(?:\.\d+)?)\s*([kKmM]?)", re.I)


def parse(question):
    m = STRIKE.search(question)
    if not m:
        return None
    val = float(m.group(2).replace(",", "")) * {"k": 1e3, "m": 1e6}.get(m.group(3).lower(), 1)
    up = m.group(1).lower() in ("reach", "hit")
    if re.search(r"\(low\)|\bdip\b|\bdrop\b|\bfall\b", question, re.I):
        up = False
    return val, up


def outcome(m):
    try:
        p = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except ValueError:
        return None
    if m.get("closed") and len(p) == 2 and max(p) >= 0.99:
        return 1 if p[0] > p[1] else 0
    return None


def p_touch(spot, strike, sigma, years, up):
    if (up and spot >= strike) or (not up and spot <= strike):
        return 1.0
    if years <= 0 or sigma <= 0:
        return 0.0
    d = abs(math.log(strike / spot)) / (sigma * math.sqrt(years))
    return min(1.0, 2 * (1 - 0.5 * (1 + math.erf(d / math.sqrt(2)))))


def price_history(token, start, end):
    """Hourly YES prices. The endpoint returns nothing for windows much longer than ~2 weeks,
    so fetch in 7-day chunks."""
    def fetch():
        out, t = [], int(start)
        while t < end:
            out += get(f"{CLOB}/prices-history", market=token, startTs=t,
                       endTs=min(int(end), t + 7 * 86400), fidelity=60).get("history", [])
            t += 7 * 86400
        return out
    h = cached(f"hist_{token}_{int(start)}.json", fetch)
    return sorted({(int(x["t"]), float(x["p"])) for x in h})


def price_at(hist, t, max_age=3 * HOUR):
    best = None
    for ts, p in hist:
        if ts > t:
            break
        best = (ts, p)
    return best[1] if best and t - best[0] <= max_age else None


def samples(asset, months, vol_days, vol_mult):
    out = []
    for year, month, ev in events(asset, months):
        start_month = datetime(year, month, 1, 4, tzinfo=timezone.utc)   # 00:00 ET ~ 04:00 UTC
        end = (datetime(year + (month == 12), month % 12 + 1, 1, 4, tzinfo=timezone.utc)).timestamp()
        klines = hourly_closes(SYMBOL[asset], start_month.timestamp() - (vol_days + 2) * 86400, end + HOUR)
        hours = sorted(klines)
        for m in ev["markets"]:
            parsed, y = parse(m.get("question", "")), outcome(m)
            if not parsed or y is None:
                continue
            strike, up = parsed
            created = datetime.fromisoformat((m.get("startDate") or m.get("createdAt")).replace("Z", "+00:00"))
            window_start = max(start_month, created).timestamp()
            token = json.loads(m["clobTokenIds"])[0]
            hist = price_history(token, window_start, end)
            t = (datetime.fromtimestamp(window_start, timezone.utc).replace(hour=12, minute=0, second=0)
                 + timedelta(days=1)).timestamp()
            while t < end - 12 * HOUR:
                past = [h for h in hours if window_start <= h < t]
                touched = any((klines[h][1] >= strike) if up else (klines[h][2] <= strike) for h in past)
                if touched:
                    break                                   # market already resolved YES
                q = price_at(hist, t)
                spot_h = max((h for h in hours if h < t), default=None)
                if q is not None and spot_h is not None and 0.02 <= q <= 0.98:
                    rets = [math.log(klines[h][0] / klines[h - HOUR][0])
                            for h in hours if t - vol_days * 86400 <= h < t and h - HOUR in klines]
                    sigma = statistics.pstdev(rets) * math.sqrt(24 * 365) * vol_mult if len(rets) > 48 else 0
                    p = p_touch(klines[spot_h][0], strike, sigma, (end - t) / (365 * 86400), up)
                    out.append({"market": m["id"], "q": m["question"], "t": t, "model": p, "mkt": q, "y": y,
                                "asset": asset, "days_left": (end - t) / 86400})
                t += 86400
    return out


def brier(rows, key):
    return sum((r[key] - r["y"]) ** 2 for r in rows) / len(rows)


def paired_z(rows):
    """Market-clustered paired test of Brier(model) - Brier(market)."""
    per = {}
    for r in rows:
        per.setdefault(r["market"], []).append((r["model"] - r["y"]) ** 2 - (r["mkt"] - r["y"]) ** 2)
    d = [sum(v) / len(v) for v in per.values()]
    if len(d) < 2:
        return 0.0, len(d)
    return statistics.mean(d) / (statistics.stdev(d) / math.sqrt(len(d))), len(d)


def trade(rows, min_edge, blend):
    """Buy the side the (blended) model favours when edge after spread+fee >= min_edge; $1 per trade,
    at most one trade per market (the first signal)."""
    done, pnl, n, wins = set(), 0.0, 0, 0
    for r in sorted(rows, key=lambda r: r["t"]):
        if r["market"] in done:
            continue
        p = r["mkt"] + blend * (r["model"] - r["mkt"])
        for side_p, price, win in ((p, r["mkt"], r["y"] == 1), (1 - p, 1 - r["mkt"], r["y"] == 0)):
            cost = price + HALF_SPREAD
            cost += FEE_RATE * cost * (1 - cost)
            if 0 < cost < 1 and side_p - cost >= min_edge:
                done.add(r["market"])
                n += 1
                wins += win
                pnl += (1 / cost if win else 0) - 1
                break
    return n, wins, pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--assets", default="bitcoin,ethereum")
    ap.add_argument("--vol-days", type=int, default=30)
    ap.add_argument("--vol-mult", type=float, default=1.0)
    a = ap.parse_args()
    rows = []
    for asset in a.assets.split(","):
        got = samples(asset, a.months, a.vol_days, a.vol_mult)
        print(f"{asset}: {len(got)} samples from {len({r['market'] for r in got})} markets", file=sys.stderr)
        rows += got
    if not rows:
        print("no samples")
        return
    z, nm = paired_z(rows)
    print(f"samples {len(rows)}  markets {nm}  vol {a.vol_days}d x{a.vol_mult}")
    print(f"Brier model {brier(rows, 'model'):.4f}  market {brier(rows, 'mkt'):.4f}  "
          f"z={z:+.2f} (negative = model better, market-clustered)")
    blend50 = [dict(r, model=0.5 * r["model"] + 0.5 * r["mkt"]) for r in rows]
    print(f"Brier 50/50 blend {brier(blend50, 'model'):.4f}  z={paired_z(blend50)[0]:+.2f}")
    for min_edge in (0.03, 0.05, 0.08):
        for blend in (0.5, 1.0):
            n, w, pnl = trade(rows, min_edge, blend)
            if n:
                print(f"trade edge>={min_edge:.2f} blend {blend:.1f}: n={n:3} won {w / n:4.0%}  ROI {pnl / n:+6.1%}")
    json.dump(rows, open(CACHE / "last_rows.json", "w"))


if __name__ == "__main__":
    main()
