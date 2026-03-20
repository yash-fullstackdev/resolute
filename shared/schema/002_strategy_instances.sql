-- Migration: Strategy Instances + Paper Trading + Daily Loss Limits
-- Allows multiple instances of the same strategy per user with different params/instruments/bias.
--
-- Backward compatible: existing rows get instance_name = strategy_name, mode stays as-is.

-- 1. Drop unique constraint so users can have multiple instances of same strategy
ALTER TABLE user_strategy_configs
  DROP CONSTRAINT IF EXISTS user_strategy_configs_tenant_id_strategy_name_key;

-- 2. Add instance-specific columns
ALTER TABLE user_strategy_configs
  ADD COLUMN IF NOT EXISTS instance_name TEXT,
  ADD COLUMN IF NOT EXISTS session TEXT NOT NULL DEFAULT 'all',
  ADD COLUMN IF NOT EXISTS max_daily_loss_pts DOUBLE PRECISION DEFAULT NULL;

-- Add check constraint for session values
DO $$ BEGIN
  ALTER TABLE user_strategy_configs
    ADD CONSTRAINT chk_session CHECK (session IN ('morning', 'afternoon', 'all'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Add check constraint for trading_mode values
DO $$ BEGIN
  ALTER TABLE user_strategy_configs
    ADD CONSTRAINT chk_trading_mode CHECK (trading_mode IN ('live', 'paper', 'disabled'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 3. Backfill existing rows: set instance_name = strategy_name where NULL
UPDATE user_strategy_configs
  SET instance_name = strategy_name
  WHERE instance_name IS NULL;

-- 4. Index for fast lookup by tenant + strategy
CREATE INDEX IF NOT EXISTS idx_usc_tenant_strategy
  ON user_strategy_configs (tenant_id, strategy_name);

-- 5. Paper trades table
CREATE TABLE IF NOT EXISTS paper_trades (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  instance_id UUID NOT NULL,
  strategy_name TEXT NOT NULL,
  instance_name TEXT,
  instrument TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
  entry_price DOUBLE PRECISION NOT NULL,
  exit_price DOUBLE PRECISION,
  stop_loss DOUBLE PRECISION NOT NULL,
  target DOUBLE PRECISION NOT NULL,
  entry_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  exit_time TIMESTAMPTZ,
  exit_reason TEXT CHECK (exit_reason IS NULL OR exit_reason IN (
    'target', 'stop_loss', 'time_stop', 'square_off', 'manual', 'daily_limit'
  )),
  pnl_points DOUBLE PRECISION,
  bias_direction TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS on paper_trades
ALTER TABLE paper_trades ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY paper_trades_tenant ON paper_trades
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Index for fast queries
CREATE INDEX IF NOT EXISTS idx_paper_trades_tenant_instance
  ON paper_trades (tenant_id, instance_id, entry_time DESC);

CREATE INDEX IF NOT EXISTS idx_paper_trades_tenant_time
  ON paper_trades (tenant_id, entry_time DESC);
