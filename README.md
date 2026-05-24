# PolySentinel — Polymarket Monitoring Bot

A prediction market monitoring system that detects price discrepancies between platforms, liquidity anomalies, and information signals. Sends alerts to Telegram.

---

## How the bot works

```
1. ingest — collects data every 5–300 seconds
   ├── Polymarket WebSocket → real-time prices → TimescaleDB
   ├── Kalshi REST poll → prices every 30s (if API key is set)
   ├── NewsAPI + RSS (BBC, NYT, Al Jazeera, etc.) → articles into Redis
   └── Gamma API → market list + metadata every 5 min

2. detectors — scans DB/Redis every 5 seconds
   ├── Arbitrage: YES+NO < $1.00 → instant alert
   ├── Liquidity: spread, book imbalance, price spike
   ├── Tail Risk: market 3–35% + LLM says "higher" → alert (every 1h)
   └── News Divergence: LLM scores news, sentiment EMA vs price (every 2 min)

3. matcher — matches markets across platforms
   └── Polymarket ↔ Kalshi / Manifold via LLM (Ollama or Claude)

4. bot — delivers alerts
   ├── Reads Redis queue
   ├── Checks deduplication, mutes, quiet hours (22:00–08:00 CET)
   └── Sends to Telegram with inline buttons
```

---

## LLM usage (Ollama + Claude)

The bot uses an LLM in three places. The model is chosen via `OLLAMA_PRIMARY`:
- `OLLAMA_PRIMARY=true` → Ollama first (local, free), Claude as fallback
- `OLLAMA_PRIMARY=false` → Claude API first, Ollama as fallback

**Recommended model:** `qwen2.5:7b-instruct-q8_0` (8 GB VRAM, excellent JSON quality)

### 1. News Divergence — article scoring

**File:** `packages/detectors/news_divergence.py`

Every 2 minutes, for each tracked market:
1. Fetches the last 5–10 articles from Redis (loaded by ingest)
2. Sends each article to LLM with a prompt:
   > "Market: {question}, current YES price: {price}. Does this news raise (R), lower (L), or not affect (U) P(YES)? Give confidence 0.0–1.0"
3. LLM returns JSON: `{"direction": "R", "confidence": 0.85, "reason": "..."}`
4. Computes EMA of sentiment across articles (alpha=0.3)
5. If `|EMA| > 0.4` and market price does not match → alert

### 2. Tail Risk — underpriced event detection

**File:** `packages/ingest/llm_prior.py`

Every 1 hour, for markets priced **3–35%** (volume >20k):
1. Passes the question text + fresh news headlines to LLM
2. LLM estimates P(YES) with confidence and reasoning
3. If LLM estimate is significantly above market price (gap ≥ 3 pp, multiplier ≥ 1.25×, confidence ≥ 0.65) → alert

Covers: geopolitics (coups, escalations), multi-outcome events (Eurovision, Oscars), regulatory surprises, crypto.

**Note:** Tail Risk only fires when fresh news is available (NewsAPI key required). Without news there will be no signals.

### 3. Market Matching — cross-platform pairing

**File:** `packages/matcher/`

Automatically finds the same question on different platforms:
1. Fetches markets from Polymarket and Kalshi/Manifold
2. Sends pairs to LLM: "Is this the same question? Confidence 0–100%"
3. Pairs with confidence ≥ 85% → `approved_by = 'auto'` (active automatically)
4. Pairs with confidence < 85% → `approved_by = 'pending'` (needs manual review)

Uses Ollama (or Claude as fallback), runs every 30 min (Kalshi) and 6 hours (Manifold).

### Current status

```
✅ enabled:   news_divergence, tail_risk, auto-matching
❌ disabled:  soft_edge (Metaculus/Manifold/Claude API calls)
❌ disabled:  llm_prior standalone scan (4h cycle)
```

Reason: API token economy. Tail risk and news divergence already use LLM efficiently.

---

## Architecture

```
Polymarket WS/REST ──┐
Manifold API         ├── ingest ──► DB (TimescaleDB) + Redis
Kalshi API (opt.)    ┘                       │
NewsAPI / RSS (BBC, NYT,                     │
  Al Jazeera, FP...)       ┌─────────────────┤
                            │                │
                        detectors         matcher
                            │                │
                      alerts → Redis  market_matches → DB
                            │
                           bot ──► Telegram
```

### Services

| Service | Port | Purpose |
|---|---|---|
| `ingest` | 8000 | Prices from Polymarket (WS + REST), Kalshi, news RSS/NewsAPI |
| `detectors` | 8001 | Signal detection every 5 sec |
| `bot` | 8002 | Telegram alert delivery, commands |
| `matcher` | 8003 | Cross-platform market pair discovery |
| `db` | 5432 | TimescaleDB — prices, markets, alerts |
| `redis` | 6379 | Alert queue, deduplication, cache |
| ~~`prometheus`~~ | 9090 | Disabled in `docker-compose.yml` — not needed for bot operation |
| ~~`grafana`~~ | 3000 | Disabled in `docker-compose.yml` — not needed for bot operation |

> Prometheus and Grafana are commented out in `docker-compose.yml` to reduce PC load. Uncomment if you need metrics dashboards.

---

## Quick start

```bash
# First time
cp .env.example .env   # fill in your tokens
docker compose build
docker compose up -d

# Check status
docker compose ps
docker compose logs -f detectors

# Rebuild after code changes (restart does NOT pick up code changes)
docker build --no-cache -t polymarket-sentinel-detectors:latest \
  -f packages/detectors/Dockerfile .
docker compose up -d --force-recreate detectors
```

> `docker compose restart` does not reload code changes — always use `build --no-cache` + `up --force-recreate`.

### If containers crash (db/redis exited 255)

This happens when the PC sleeps — Docker Desktop VM is killed. Recovery:

```bash
docker compose up -d
```

Permanent fix: Docker Desktop → Settings → Resources → uncheck **"Enable Resource Saver"**. This is the main crash cause — the VM is paused when idle.

---

## Environment variables (`.env`)

```env
# Telegram
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_ID=...              # your chat_id (get via @userinfobot)

# Ollama (primary LLM — free, local)
OLLAMA_BASE_URL=http://192.168.65.2:11434   # host address inside Docker Desktop VM
OLLAMA_MODEL=qwen2.5:7b-instruct-q8_0       # recommended for 8 GB VRAM
OLLAMA_PRIMARY=true

# Claude API (optional — fallback if Ollama is unavailable)
ANTHROPIC_API_KEY=                           # leave empty to disable

# Kalshi (optional — required for cross-platform arb)
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=

# Metaculus (optional — community predictions)
METACULUS_API_TOKEN=

# News (required for tail risk + news divergence)
NEWSAPI_KEY=

# Alert thresholds
ARB_MIN_EDGE_BPS=100          # min cross-platform arb edge (1 pp)
SOFT_EDGE_MIN_BPS=500         # min relative edge for SOFT EDGE (5%)
SOFT_EDGE_MIN_PP=5.0          # min absolute gap in percentage points
LIQUIDITY_SPREAD_THRESHOLD=0.15
LIQUIDITY_MIN_MID=0.10        # ignore markets below 10¢ or above 90¢

# Ingest
TOP_MARKETS_BY_VOLUME=500
MAX_MARKETS_PER_EVENT=8       # max markets per event (diversity filter)

# Supplemental markets: always tracked regardless of volume rank
# Add market_ids comma-separated. After changing: restart ingest only (no rebuild needed).
SUPPLEMENTAL_MARKET_IDS=701539,701540,...   # e.g. ETH EOY 2026
```

> After changing `SUPPLEMENTAL_MARKET_IDS`, only `docker compose up -d ingest` is needed — no image rebuild required.

---

## Signal sources

| Source | Reliability | Notes |
|---|---|---|
| **Kalshi** (cross-platform arb) | ★★★★★ | Real money, mechanical profit. Requires API key |
| **Intra-market arb** | ★★★★★ | YES+NO inside Polymarket < $1. No extra keys needed |
| **Metaculus** | ★★★★☆ | Community CP, real forecasters. Requires API token above free tier |
| **PredictIt** | ★★★☆☆ | Real money. −10% fee on profit. Geo-blocked outside the US |
| **Tail Risk (LLM + News)** | ★★★☆☆ | Underpriced events 3–35%. Runs on Ollama; fresh news recommended for signals |
| **News Divergence** | ★★★☆☆ | News sentiment vs market price. Works well on macro/crypto |
| **Manifold** | ★★☆☆☆ | Play-money (Mana). Frequent recency/partisan bias |
| **LLM Prior (Ollama/Claude)** | ★★☆☆☆ | P(YES) estimate via LLM. Supporting signal only |

---

## Alert types

### ⚡ CROSS-PLATFORM ARB

**What it is:** Guaranteed arbitrage — buy YES on Polymarket + NO on Kalshi for less than $1 combined. Profit regardless of outcome.

**Requires:** Kalshi API key

```
⚡ CROSS-PLATFORM ARB  +150 bps (1.5%)
Will OKC Thunder win the 2026 NBA Finals?

BUY YES @ Polymarket  avg 49.1¢
BUY NO  @ Kalshi      avg 50.2¢

For 100 contracts:
  Outlay → $99.30   Payout → $100.00
  Profit: +$0.70
```

**Action:** Buy both sides simultaneously. Kalshi charges ~1% fee — already factored into the calculation.

---

### 🔄 INTRA-MARKET ARB

**What it is:** YES + NO inside a single Polymarket market cost less than $1 combined.

```
🔄 INTRA-MARKET ARB  +80 bps (0.8%)
Will Argentina win the 2026 FIFA World Cup?

YES+NO bundle cheaper than $1:
  YES 48.8¢  +  NO 50.7¢

For 100 contracts:
  Outlay → $99.50   Payout → $100.00
  Profit: +$0.50
```

**Action:** Buy both YES and NO tokens simultaneously. The pair always pays $1 at resolution.

---

### 🎯 TAIL RISK — UNDERPRICED

**What it is:** A market priced 3–35% is underpriced according to fresh news. The LLM analyzes recent headlines and signals only when concrete facts support it (confidence ≥ 0.65). Runs every 1 hour.

```
🎯 TAIL RISK — UNDERPRICED
Will Bulgaria win Eurovision 2026?

📈 BUY YES
Market: 6.5¢  LLM: 15.0%  (confidence 72%)
Gap: +8.5 pp  EV per $1: +1.31
Kelly ½: 2.8% of bankroll

💡 Bulgaria has won the national selection with a strong jury favorite track
🤖 Verify with news before acting
```

**Fields:**
- `confidence` — quality of news coverage: ≥0.80 = fresh concrete facts, 0.65–0.79 = moderate data
- `Kelly ½` — conservative position size (½ of full Kelly, since this is an LLM estimate, not mechanical arb)
- `⚡ Order flow confirms` — appears if there was a Price Spike or Book Imbalance on the same market in the last hour

**When to trade:** gap ≥ 10 pp + confidence ≥ 0.80 + verify news manually.

---

### 📊 / 🤖 / 🏛️ SOFT EDGE

**What it is:** Polymarket price differs from an external platform's probability or LLM estimate.

```
📊 SOFT EDGE — MANIFOLD
Will the Republicans win the 2028 US Presidential Election?

📈 Under-priced → BUY YES
Manifold: 59.1%  Market: 38.5¢
Gap: +20.6 pp  EV per $1: +0.536
Kelly ¼: 4.2% of bankroll
⚠️ Manifold = play money (Mana) — Kelly already halved
✅ Rules align
```

**Fields:**
- `Gap: ±X pp` — difference in percentage points
- `EV per $1` — expected profit per dollar wagered
- `Kelly ¼: X%` — recommended position size (¼ of full Kelly)

**When to trade by source:**

| Source | Action |
|---|---|
| Metaculus CP (🎯) | Trade at gap ≥ 5 pp, Kelly ≥ 0.5% |
| PredictIt (🏛️) | Trade at gap ≥ 5 pp (account for −10% fee) |
| Manifold (📊) | Check manually, small position (½ of Kelly) |
| LLM Prior (🤖) | Information only, do not trade automatically |

**Position size:** `Kelly ¼ (%) × bankroll`. At $1000 and Kelly 2.1% → bet $21.

---

### 📰 NEWS DIVERGENCE

**What it is:** Recent news sentiment diverges from market price. The detector reads the latest articles for the market, scores each one (−1..+1), computes EMA sentiment, and compares with market price.

```
📰 NEWS DIVERGENCE  (conf 85%)
Will no Fed rate cuts happen in 2026?

📉 Bearish — news more negative than price implies → consider BUY NO
Market: 50.0¢  Sentiment EMA: -0.518 (negative)
Articles scored: 10

💡 Higher inflation (3.5% core) reduces probability of zero cuts...
  • Core Inflation Rate Jumps to Its Highest in Years Thanks to Iran War
🤖 Verify news before acting
```

**Fields:**
- `Sentiment EMA` — exponential moving average of article scores (negative = bearish tone)
- `conf X%` — signal confidence (≥80% = strong signal)
- `Bearish / Bullish` — direction: Bearish → market overpriced (BUY NO), Bullish → market underpriced (BUY YES)

**When to trade:**
- Confidence ≥ 80%
- Sentiment EMA < −0.40 (Bearish) or > +0.40 (Bullish)
- Aligns with another signal (Soft Edge or Tail Risk)
- Verify manually: news is current, not stale

> News source is NewsAPI + RSS. Irrelevant articles may appear. Always check the `reason` and headlines before trading.

---

## Market pairs (market_matches)

### Manual pairs (`manual_map.yaml`)

```yaml
markets:
  - key: my_market_key
    polymarket: "123456"             # market_id from DB
    manifold: "some-manifold-slug"   # slug from manifold.markets/... URL
    # manifold: "slug#AnswerText"    # for multi-choice markets
    metaculus: 12345                 # question ID
    predictit: "market_id/contract_id"
    notes: "Notes on resolution rule differences"

  # Standalone entry (Polymarket only) — appears in /list and news_divergence,
  # but does not generate cross-platform soft_edge (no external pair to compare)
  - key: eth_dip_1500_dec2026
    polymarket: "701552"
    notes: "ETH dip to $1,500 by Dec 31, 2026. (844k vol)"
```

**Covered markets:**
- Geopolitics: Iran, Ukraine, China-Taiwan, Israel-Saudi Arabia
- Macro: Fed (2026 rates), US recession
- Crypto: **16 ETH EOY 2026** (`eth_reach_3500…10000`, `eth_dip_800…2500`), BTC $150k
- Sports: NBA Finals 2026 (OKC), FIFA World Cup 2026 (France, Argentina, Brazil)
- AI: OpenAI IPO, best AI model (Anthropic vs OpenAI vs xAI)
- US Politics: 2028 elections (Trump, Vance, Newsom)

After changing the YAML — always rebuild:

```bash
docker compose build matcher
docker compose up -d matcher
```

> `docker compose restart matcher` **will not work** — the YAML is baked into the image at build time.

### Auto-matching

| Pair | Interval | LLM |
|---|---|---|
| Polymarket ↔ Kalshi | every 30 min | Ollama (fallback: Claude) |
| Polymarket ↔ Manifold | every 6 hours | Ollama (fallback: Claude) |

Pairs with confidence ≥ 85% are activated automatically (`approved_by = 'auto'`).
Pairs with confidence < 85% → `approved_by = 'pending'` (manual review needed).

---

## Telegram commands

| Command | Description |
|---|---|
| `/start` | List all commands |
| `/status` | Ingest health + last alert |
| `/positions` | Current Polymarket portfolio |
| `/list` | Watched markets + thresholds (paginated if > ~70) |
| `/watch <slug>` | Add a market by slug or group key |
| `/unwatch <slug>` | Remove a market from the watchlist |
| `/pause <2h \| 30m \| 1d>` | Mute all alerts for a period |
| `/resume` | Resume paused alerts |
| `/threshold arb <bps>` | Change arbitrage threshold |
| `/threshold soft <bps>` | Change soft edge threshold |
| `/calibration` | Model accuracy (Brier score) |
| `/explain <alert_id>` | Detailed breakdown of an alert by ID |

`group_key` is visible in each alert or in DB: `SELECT DISTINCT group_key FROM alerts`.

Buttons under each alert:
- **Polymarket ↗** — open market page
- **Mute 1h** / **Mute forever** — silence this market

---

## Alert deduplication

Repeated alerts of the same type for the same market are suppressed. The dedup key for fundamental signals uses only `kind:group_key` (without edge size), so ±50 bps drift does not generate duplicates.

| Type | Min interval | Edge in key? |
|---|---|---|
| `arb_xplatform`, `arb_intramarket` | 30 min | ✅ (50 bps bucket) |
| `soft_edge_predictit` | 2 hours | ❌ |
| `soft_edge_manifold`, `soft_edge_metaculus` | 4 hours | ❌ |
| `soft_edge_llm_prior` | 6 hours | ❌ |
| `tail_risk` | 6 hours | ❌ |
| `news_divergence` | 12 hours | ❌ |

---

## Monitoring

```bash
# Live logs
docker compose logs -f detectors
docker compose logs -f bot

# All alerts from the last hour
docker exec polymarket-sentinel-db-1 psql -U postgres polysentinel \
  -c "SELECT kind, group_key, edge_bps, payload->>'title', ts
      FROM alerts ORDER BY ts DESC LIMIT 20;"

# Active market pairs
docker exec polymarket-sentinel-db-1 psql -U postgres polysentinel \
  -c "SELECT group_key, source, source_id, approved_by
      FROM market_matches ORDER BY group_key, source;"

# Container memory usage
docker stats --no-stream

# LLM prior cache
docker exec polymarket-sentinel-redis-1 redis-cli KEYS "llm_prior:*"
```

### Container memory limits

Docker Desktop VM is recommended to be limited to **8–16 GB** (Settings → Resources → Memory).

| Container | Actual peak | Limit |
|---|---|---|
| `db` | ~400 MB | 2 GB |
| `redis` | 512 MB (self-capped) | 768 MB |
| `ingest` | ~150 MB | 768 MB |
| `detectors` | ~120 MB | 768 MB |
| `bot` | ~200 MB | 512 MB |
| `matcher` | ~2.4 GB (on embedding run) | 8 GB |
| **Total actual** | **~3.6 GB** | — |

---

## Trading rules

1. **Arb — always trade** at edge ≥ 1 pp; outcome-independent mechanical profit
2. **SOFT EDGE — trade only if** gap ≥ 5 pp, Kelly ≥ 0.5%, mid between 15¢ and 85¢
3. **TAIL RISK — trade only if** gap ≥ 10 pp + confidence ≥ 0.80 + verify news manually
4. **NEWS DIVERGENCE — trade only if** conf ≥ 80% + sentiment EMA > 0.40 + aligns with another signal
5. **Check notes in YAML** — different resolution rules make comparisons meaningless
6. **Manifold on distant-horizon politics (>1 year) — do not trade**: recency bias and no financial incentive
7. **LLM Prior — information only**, never the sole basis for a trade
8. **Kelly ¼** — conservative estimate. Never bet full Kelly on a play-money source
9. **Mute noisy markets**: `/mute <group_key> forever`
10. **Positional bets (tail risk, ETH)** — horizon of weeks/months. Price moves at playoff round results, key news, or resolution. Do not sell on ±2% noise.

### "Before GTA VI" markets (Jesus, Russia-Ukraine ceasefire, etc.)

These are an indirect bet on the GTA VI release date (~fall 2026). NO means "GTA VI releases before X happens." If GTA VI release is confirmed → all NO positions approach resolution. YES = indefinite delay.
