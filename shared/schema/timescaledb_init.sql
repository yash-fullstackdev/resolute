-- =============================================================================
-- TimescaleDB Initialization Script
-- Multi-tenant schema with PostgreSQL Row-Level Security (RLS)
-- =============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Application roles
-- app_user: used by all services
-- rls_bypass: used only for admin operations that need to see all tenant data
CREATE ROLE app_user;
CREATE ROLE rls_bypass BYPASSRLS;

-- Tenants (SaaS users)
CREATE TABLE tenants (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email                TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    subscription_tier    TEXT NOT NULL DEFAULT 'TRIAL',
    subscription_status  TEXT NOT NULL DEFAULT 'TRIAL',
    trial_ends_at        TIMESTAMPTZ,
    subscription_ends_at TIMESTAMPTZ,
    email_verified       BOOLEAN NOT NULL DEFAULT FALSE,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON tenants (email);
CREATE INDEX ON tenants (subscription_status);

-- Refresh tokens (for JWT rotation)
CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON refresh_tokens (tenant_id, expires_at);

-- Broker credentials (encrypted at rest)
CREATE TABLE user_broker_credentials (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    broker           TEXT NOT NULL,
    is_primary       BOOLEAN NOT NULL DEFAULT TRUE,
    api_key_enc      BYTEA NOT NULL,
    api_secret_enc   BYTEA NOT NULL,
    client_id_enc    BYTEA NOT NULL,
    totp_secret_enc  BYTEA,
    access_token_enc BYTEA,
    token_expires_at TIMESTAMPTZ,
    is_verified      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, broker)
);
-- No RLS on this table: accessed ONLY via auth_service internal endpoint over mTLS

-- Per-user strategy configuration
CREATE TABLE user_strategy_configs (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    strategy_name            TEXT NOT NULL,
    enabled                  BOOLEAN NOT NULL DEFAULT FALSE,
    params                   JSONB NOT NULL DEFAULT '{}',
    portfolio_value_inr      DOUBLE PRECISION NOT NULL DEFAULT 500000,
    max_risk_per_trade_pct   DOUBLE PRECISION NOT NULL DEFAULT 2.0,
    trading_mode             TEXT NOT NULL DEFAULT 'paper',
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, strategy_name)
);
ALTER TABLE user_strategy_configs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON user_strategy_configs
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);

-- Shared market data (no tenant_id, no RLS needed)
CREATE TABLE ticks (
    time             TIMESTAMPTZ NOT NULL,
    symbol           TEXT NOT NULL,
    segment          TEXT NOT NULL,
    last_price       DOUBLE PRECISION,
    bid              DOUBLE PRECISION,
    ask              DOUBLE PRECISION,
    volume           BIGINT,
    oi               BIGINT,
    strike           DOUBLE PRECISION,
    option_type      TEXT,
    expiry           DATE,
    underlying_price DOUBLE PRECISION
);
SELECT create_hypertable('ticks', 'time');
CREATE INDEX ON ticks (symbol, time DESC);

-- Shared market data (no tenant_id)
CREATE TABLE atm_iv_snapshots (
    time          TIMESTAMPTZ NOT NULL,
    underlying    TEXT NOT NULL,
    atm_iv        DOUBLE PRECISION,
    iv_rank       DOUBLE PRECISION,
    iv_percentile DOUBLE PRECISION,
    pcr_oi        DOUBLE PRECISION,
    pcr_volume    DOUBLE PRECISION,
    vix           DOUBLE PRECISION
);
SELECT create_hypertable('atm_iv_snapshots', 'time');
CREATE INDEX ON atm_iv_snapshots (underlying, time DESC);

-- Per-tenant tables: all have tenant_id + RLS
-- Set tenant context before every query:
--   SET LOCAL app.current_tenant_id = '<uuid>';

CREATE TABLE signals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id),
    time              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy          TEXT NOT NULL,
    underlying        TEXT NOT NULL,
    segment           TEXT NOT NULL,
    direction         TEXT NOT NULL,
    strength          DOUBLE PRECISION,
    regime            TEXT,
    legs              JSONB,
    max_loss_inr      DOUBLE PRECISION,
    target_profit_inr DOUBLE PRECISION,
    stop_loss_pct     DOUBLE PRECISION,
    time_stop         TIMESTAMPTZ,
    rationale         TEXT,
    acted_upon        BOOLEAN DEFAULT FALSE
);
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON signals
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON signals (tenant_id, strategy, time DESC);

CREATE TABLE positions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    signal_id        UUID REFERENCES signals(id),
    strategy         TEXT NOT NULL,
    underlying       TEXT NOT NULL,
    segment          TEXT NOT NULL,
    legs             JSONB,
    entry_time       TIMESTAMPTZ NOT NULL,
    exit_time        TIMESTAMPTZ,
    entry_cost_inr   DOUBLE PRECISION,
    exit_value_inr   DOUBLE PRECISION,
    realised_pnl_inr DOUBLE PRECISION,
    stop_loss_price  DOUBLE PRECISION NOT NULL,
    target_price     DOUBLE PRECISION NOT NULL,
    time_stop        TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL,
    exit_reason      TEXT,
    greeks_at_entry  JSONB,
    greeks_at_exit   JSONB
);
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON positions
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON positions (tenant_id, strategy, entry_time DESC);
CREATE INDEX ON positions (tenant_id, status, entry_time DESC);

CREATE TABLE orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    position_id     UUID REFERENCES positions(id),
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    action          TEXT NOT NULL,
    quantity        INTEGER,
    lot_size        INTEGER,
    order_type      TEXT,
    limit_price     DOUBLE PRECISION,
    fill_price      DOUBLE PRECISION,
    fill_time       TIMESTAMPTZ,
    status          TEXT NOT NULL,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON orders
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON orders (tenant_id, created_at DESC);

CREATE MATERIALIZED VIEW daily_performance AS
SELECT
    tenant_id,
    DATE(entry_time)  AS trade_date,
    strategy,
    segment,
    COUNT(*)          AS total_trades,
    COUNT(*) FILTER (WHERE realised_pnl_inr > 0)  AS wins,
    COUNT(*) FILTER (WHERE realised_pnl_inr <= 0) AS losses,
    SUM(realised_pnl_inr)                         AS total_pnl_inr,
    AVG(realised_pnl_inr) FILTER (WHERE realised_pnl_inr > 0)  AS avg_win_inr,
    AVG(realised_pnl_inr) FILTER (WHERE realised_pnl_inr <= 0) AS avg_loss_inr,
    MAX(realised_pnl_inr) AS best_trade_inr,
    MIN(realised_pnl_inr) AS worst_trade_inr
FROM positions
WHERE status IN ('CLOSED', 'STOP_HIT', 'TIME_STOP', 'TARGET_HIT')
GROUP BY tenant_id, trade_date, strategy, segment
WITH NO DATA;
CREATE UNIQUE INDEX ON daily_performance (tenant_id, trade_date, strategy, segment);

-- Discipline tables (all with tenant_id + RLS)
CREATE TABLE trading_plans (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_date               DATE NOT NULL,
    enabled_strategies      JSONB NOT NULL,
    active_underlyings      JSONB NOT NULL,
    max_trades_per_day      INTEGER NOT NULL DEFAULT 5,
    daily_loss_limit_inr    DOUBLE PRECISION NOT NULL,
    daily_profit_target_inr DOUBLE PRECISION,
    notes                   TEXT,
    status                  TEXT NOT NULL DEFAULT 'DRAFT',
    locked_at               TIMESTAMPTZ,
    plan_hash               TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, plan_date)
);
ALTER TABLE trading_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON trading_plans
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON trading_plans (tenant_id, plan_date DESC);

CREATE TABLE circuit_breaker_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL,
    trigger_reason   TEXT,
    pnl_at_event_inr DOUBLE PRECISION,
    trades_at_event  INTEGER,
    event_time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE circuit_breaker_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON circuit_breaker_events
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON circuit_breaker_events (tenant_id, event_time DESC);

CREATE TABLE override_audit_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    position_id         UUID REFERENCES positions(id),
    override_type       TEXT NOT NULL,
    original_value      DOUBLE PRECISION,
    proposed_value      DOUBLE PRECISION,
    reason              TEXT NOT NULL,
    requested_at        TIMESTAMPTZ NOT NULL,
    cooldown_expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL,
    outcome_pnl_inr     DOUBLE PRECISION,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE override_audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON override_audit_log
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON override_audit_log (tenant_id, requested_at DESC);

CREATE TABLE trade_journal (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    position_id         UUID REFERENCES positions(id) UNIQUE,
    trade_date          DATE NOT NULL,
    strategy            TEXT NOT NULL,
    underlying          TEXT NOT NULL,
    segment             TEXT NOT NULL,
    entry_time          TIMESTAMPTZ NOT NULL,
    exit_time           TIMESTAMPTZ NOT NULL,
    exit_reason         TEXT NOT NULL,
    entry_cost_inr      DOUBLE PRECISION NOT NULL,
    exit_value_inr      DOUBLE PRECISION NOT NULL,
    realised_pnl_inr    DOUBLE PRECISION NOT NULL,
    pnl_pct             DOUBLE PRECISION NOT NULL,
    was_in_plan         BOOLEAN NOT NULL,
    stop_loss_respected BOOLEAN NOT NULL,
    time_stop_respected BOOLEAN NOT NULL,
    override_count      INTEGER NOT NULL DEFAULT 0,
    discipline_score    DOUBLE PRECISION NOT NULL,
    pre_market_thesis   TEXT,
    post_trade_notes    TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('trade_journal', 'entry_time');
ALTER TABLE trade_journal ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON trade_journal
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON trade_journal (tenant_id, trade_date DESC);
CREATE INDEX ON trade_journal (tenant_id, discipline_score);

CREATE MATERIALIZED VIEW user_discipline_scores AS
SELECT
    tenant_id,
    COUNT(*)                                                     AS total_trades,
    ROUND(AVG(discipline_score)::numeric, 1)                     AS rolling_score,
    COUNT(*) FILTER (WHERE discipline_score >= 75)               AS disciplined_trades,
    COUNT(*) FILTER (WHERE discipline_score < 50)                AS undisciplined_trades,
    SUM(realised_pnl_inr) FILTER (WHERE discipline_score >= 75) AS pnl_disciplined_inr,
    SUM(realised_pnl_inr) FILTER (WHERE discipline_score < 50)  AS pnl_undisciplined_inr,
    SUM(override_count)                                          AS total_overrides,
    MAX(entry_time)                                              AS last_trade_at
FROM trade_journal
WHERE entry_time >= NOW() - INTERVAL '30 days'
GROUP BY tenant_id
WITH NO DATA;
CREATE UNIQUE INDEX ON user_discipline_scores (tenant_id);

-- Audit events (immutable — no RLS, admin-only access)
CREATE TABLE audit_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type  TEXT NOT NULL,
    tenant_id   UUID REFERENCES tenants(id),
    details     JSONB NOT NULL,
    ip_address  INET,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON audit_events (tenant_id, created_at DESC);
CREATE INDEX ON audit_events (event_type, created_at DESC);

-- Prevent modification of audit events (immutable log)
CREATE OR REPLACE FUNCTION prevent_audit_modification() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Audit events are immutable — cannot modify or delete';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER no_audit_update BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

-- Custom AI strategies (per tenant)
CREATE TABLE custom_strategies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT,
    category            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'DRAFT',
    target_symbols      JSONB NOT NULL,
    target_segments     JSONB NOT NULL,
    indicators          JSONB NOT NULL,
    entry_conditions    JSONB NOT NULL,
    exit_conditions     JSONB NOT NULL,
    option_action       TEXT NOT NULL,
    strike_selection    TEXT NOT NULL,
    delta_target        DOUBLE PRECISION,
    dte_min             INTEGER NOT NULL,
    dte_max             INTEGER NOT NULL,
    spread_config       JSONB,
    stop_loss_pct       DOUBLE PRECISION NOT NULL,
    profit_target_pct   DOUBLE PRECISION NOT NULL,
    time_stop_rule      TEXT NOT NULL,
    time_stop_value     TEXT,
    max_positions_per_symbol INTEGER NOT NULL DEFAULT 1,
    backtest_results    JSONB,
    ai_review_notes     TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, name)
);
ALTER TABLE custom_strategies ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON custom_strategies
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);
CREATE INDEX ON custom_strategies (tenant_id, status);

-- Indicator OHLCV candle buffer (shared market data, no tenant_id)
CREATE TABLE indicator_candles (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      BIGINT
);
SELECT create_hypertable('indicator_candles', 'time');
CREATE INDEX ON indicator_candles (symbol, timeframe, time DESC);

-- Platform admin view (requires rls_bypass role)
CREATE VIEW platform_summary AS
SELECT
    t.id            AS tenant_id,
    t.email,
    t.subscription_tier,
    t.subscription_status,
    COUNT(p.id)     AS total_positions,
    SUM(p.realised_pnl_inr) FILTER (WHERE p.status != 'OPEN') AS total_pnl_inr,
    MAX(p.entry_time) AS last_trade_at
FROM tenants t
LEFT JOIN positions p ON p.tenant_id = t.id
GROUP BY t.id, t.email, t.subscription_tier, t.subscription_status;
