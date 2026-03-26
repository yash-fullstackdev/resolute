-- Migration: Equity Momentum — snapshots hypertable, instruments master, signals metadata
-- Adds shared market-data tables for equity intraday trading (no RLS, no tenant_id).
-- Idempotent: safe to re-run.

-- =============================================================================
-- 1. equity_snapshots — 1-minute enriched OHLCV for equity stocks (shared)
-- =============================================================================
CREATE TABLE IF NOT EXISTS equity_snapshots (
    time              TIMESTAMPTZ      NOT NULL,
    trading_date      DATE             NOT NULL,
    symbol            TEXT             NOT NULL,
    security_id       TEXT             NOT NULL,
    bucket            SMALLINT         NOT NULL,
    ltp               DOUBLE PRECISION,
    open_price        DOUBLE PRECISION,
    prev_close        DOUBLE PRECISION,
    candle_open       DOUBLE PRECISION,
    candle_high       DOUBLE PRECISION,
    candle_low        DOUBLE PRECISION,
    volume_cum        BIGINT,
    bid               DOUBLE PRECISION,
    ask               DOUBLE PRECISION,
    bid_qty           INTEGER,
    ask_qty           INTEGER,
    volume_delta      INTEGER,
    vwap              DOUBLE PRECISION,
    spread_pct        DOUBLE PRECISION,
    price_velocity    DOUBLE PRECISION,
    volume_rate       DOUBLE PRECISION,
    candle_body_ratio DOUBLE PRECISION
);

SELECT create_hypertable('equity_snapshots', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_eq_snap_date_symbol_bucket
    ON equity_snapshots (trading_date, symbol, bucket);

-- Compression: segment by symbol, order by time
ALTER TABLE equity_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('equity_snapshots', INTERVAL '7 days', if_not_exists => TRUE);

-- =============================================================================
-- 2. equity_instruments — master stock list (shared, no RLS)
-- =============================================================================
CREATE TABLE IF NOT EXISTS equity_instruments (
    security_id  TEXT        PRIMARY KEY,
    symbol       TEXT        NOT NULL UNIQUE,
    company_name TEXT,
    tiers        TEXT[]      DEFAULT '{}',
    enabled      BOOLEAN     DEFAULT TRUE,
    min_volume   INTEGER     DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eq_instr_enabled_tiers
    ON equity_instruments (enabled, tiers);

-- =============================================================================
-- 3. signals.metadata — JSONB column for strategy-specific context
-- =============================================================================
ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS metadata JSONB;
