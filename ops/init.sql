-- Polysentinel DB schema
-- Run automatically by TimescaleDB-HA container on first start

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Markets ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS markets (
  market_id         text PRIMARY KEY,
  source            text NOT NULL,          -- 'polymarket' | 'kalshi' | 'manifold' | 'metaculus' | 'predictit'
  condition_id      text,                   -- polymarket condition_id
  question          text,
  description       text,
  slug              text,
  tags              text[],
  end_date          timestamptz,
  tick_size         numeric,
  min_order_size    numeric,
  fee_schedule      jsonb,
  active            boolean DEFAULT true,
  raw               jsonb,                  -- full source JSON snapshot
  created_at        timestamptz DEFAULT now(),
  updated_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS markets_source_idx   ON markets (source);
CREATE INDEX IF NOT EXISTS markets_slug_idx     ON markets (slug);
CREATE INDEX IF NOT EXISTS markets_active_idx   ON markets (active) WHERE active = true;

-- ── Tokens (Polymarket YES/NO per market) ──────────────────────────────────

CREATE TABLE IF NOT EXISTS tokens (
  token_id    text PRIMARY KEY,
  market_id   text REFERENCES markets (market_id) ON DELETE CASCADE,
  outcome     text NOT NULL,               -- 'YES' | 'NO'
  created_at  timestamptz DEFAULT now()
);

-- ── Price time-series (hypertable) ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS prices (
  ts              timestamptz NOT NULL,
  token_id        text        NOT NULL,
  source          text        NOT NULL,
  best_bid        numeric,
  best_ask        numeric,
  mid             numeric,
  last_trade      numeric,
  bid_size_top    numeric,
  ask_size_top    numeric,
  liquidity       numeric,
  volume_24h      numeric
);

SELECT create_hypertable('prices', 'ts', if_not_exists => true);
CREATE INDEX IF NOT EXISTS prices_token_ts ON prices (token_id, ts DESC);

ALTER TABLE prices SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'token_id,source'
);

SELECT add_compression_policy('prices', INTERVAL '7 days', if_not_exists => true);

-- Continuous aggregate: 1-minute OHLC
CREATE MATERIALIZED VIEW IF NOT EXISTS prices_1m
WITH (timescaledb.continuous) AS
  SELECT
    time_bucket('1 minute', ts) AS bucket,
    token_id,
    source,
    first(mid, ts)  AS open,
    max(mid)        AS high,
    min(mid)        AS low,
    last(mid, ts)   AS close,
    avg(mid)        AS avg_mid,
    count(*)        AS ticks
  FROM prices
  GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('prices_1m',
  start_offset   => INTERVAL '1 hour',
  end_offset     => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute',
  if_not_exists => true
);

-- ── Market matches (cross-platform) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS market_matches (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_key       text NOT NULL,
  source          text NOT NULL,
  source_id       text NOT NULL,
  side            text NOT NULL DEFAULT 'YES',  -- 'YES' | 'NO' (inverted polarity)
  match_score     float,
  llm_confidence  float,
  rule_notes      text,
  approved_by     text DEFAULT 'pending',       -- 'auto' | 'manual' | 'pending'
  updated_at      timestamptz DEFAULT now(),
  UNIQUE (group_key, source)
);

CREATE INDEX IF NOT EXISTS mm_group_key_idx ON market_matches (group_key);
CREATE INDEX IF NOT EXISTS mm_approved_idx  ON market_matches (approved_by);

-- ── Alerts ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS alerts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts          timestamptz DEFAULT now(),
  kind        text NOT NULL,    -- 'arb_xplatform' | 'arb_intramarket' | 'soft_edge' | 'liquidity'
  group_key   text,
  payload     jsonb,
  edge_bps    int,
  status      text DEFAULT 'pending'  -- 'pending' | 'sent' | 'muted'
);

CREATE INDEX IF NOT EXISTS alerts_ts_idx       ON alerts (ts DESC);
CREATE INDEX IF NOT EXISTS alerts_group_key_idx ON alerts (group_key);
CREATE INDEX IF NOT EXISTS alerts_kind_idx     ON alerts (kind);

-- ── Model calibration snapshots ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calibration_snapshots (
  ts          timestamptz DEFAULT now(),
  market_id   text,
  model_p     float,
  market_p    float,
  model_kind  text,
  outcome     int,   -- NULL until resolved; 0 or 1 after
  PRIMARY KEY (ts, market_id, model_kind)
);

SELECT create_hypertable('calibration_snapshots', 'ts', if_not_exists => true);

-- ── Mute list ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mutes (
  group_key   text NOT NULL,
  muted_until timestamptz NOT NULL,
  PRIMARY KEY (group_key)
);
