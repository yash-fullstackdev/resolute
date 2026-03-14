-- Seed dummy data for demo tenant
-- tenant_id: 6105f2e9-dd72-4dce-8791-76e7a267443e

-- Set RLS context
SET LOCAL app.current_tenant_id = '6105f2e9-dd72-4dce-8791-76e7a267443e';

-- ============================================================================
-- 3 Signals (different strategies, recent timestamps)
-- ============================================================================
INSERT INTO signals (id, tenant_id, time, strategy, underlying, segment, direction, strength, regime, legs, max_loss_inr, target_profit_inr, stop_loss_pct, time_stop, rationale, acted_upon)
VALUES
(
    'a1b2c3d4-1111-4aaa-bbbb-000000000001',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NOW() - INTERVAL '2 hours',
    'long_call',
    'NIFTY',
    'NSE_FO',
    'BUY',
    0.82,
    'BULL_LOW_VOL',
    '[{"symbol": "NIFTY24MAR23500CE", "exchange": "NFO", "action": "BUY", "quantity": 50, "lot_size": 50}]'::jsonb,
    5000.0,
    10000.0,
    20.0,
    NOW() + INTERVAL '6 hours',
    'RSI oversold bounce with MACD bullish crossover on NIFTY. IV Rank at 35 supports buying.',
    TRUE
),
(
    'a1b2c3d4-2222-4aaa-bbbb-000000000002',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NOW() - INTERVAL '1 hour',
    'iron_condor',
    'BANKNIFTY',
    'NSE_FO',
    'SELL',
    0.75,
    'BULL_LOW_VOL',
    '[{"symbol": "BANKNIFTY24MAR49000CE", "exchange": "NFO", "action": "SELL", "quantity": 15, "lot_size": 15}, {"symbol": "BANKNIFTY24MAR48000PE", "exchange": "NFO", "action": "SELL", "quantity": 15, "lot_size": 15}]'::jsonb,
    8000.0,
    6000.0,
    25.0,
    NOW() + INTERVAL '5 hours',
    'IV Rank above 55, ADX below 20 indicating range-bound market. Iron condor setup.',
    TRUE
),
(
    'a1b2c3d4-3333-4aaa-bbbb-000000000003',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NOW() - INTERVAL '30 minutes',
    'supertrend_momentum',
    'NIFTY',
    'NSE_FO',
    'BUY',
    0.68,
    'BULL_LOW_VOL',
    '[{"symbol": "NIFTY24MAR23600CE", "exchange": "NFO", "action": "BUY", "quantity": 50, "lot_size": 50}]'::jsonb,
    4500.0,
    9000.0,
    18.0,
    NOW() + INTERVAL '4 hours',
    'SuperTrend buy signal confirmed with VWAP support. Volume above average.',
    FALSE
);

-- ============================================================================
-- 3 Positions (2 open, 1 closed)
-- ============================================================================
INSERT INTO positions (id, tenant_id, signal_id, strategy, underlying, segment, legs, entry_time, exit_time, entry_cost_inr, exit_value_inr, realised_pnl_inr, stop_loss_price, target_price, time_stop, status, exit_reason, greeks_at_entry)
VALUES
(
    'b2c3d4e5-1111-4bbb-cccc-000000000001',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'a1b2c3d4-1111-4aaa-bbbb-000000000001',
    'long_call',
    'NIFTY',
    'NSE_FO',
    '[{"symbol": "NIFTY24MAR23500CE", "exchange": "NFO", "action": "BUY", "quantity": 50, "lot_size": 50}]'::jsonb,
    NOW() - INTERVAL '2 hours',
    NULL,
    12500.0,
    NULL,
    0.0,
    200.0,
    300.0,
    NOW() + INTERVAL '6 hours',
    'OPEN',
    NULL,
    '{"delta": 0.55, "gamma": 0.003, "theta": -45.2, "vega": 12.5}'::jsonb
),
(
    'b2c3d4e5-2222-4bbb-cccc-000000000002',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'a1b2c3d4-2222-4aaa-bbbb-000000000002',
    'iron_condor',
    'BANKNIFTY',
    'NSE_FO',
    '[{"symbol": "BANKNIFTY24MAR49000CE", "exchange": "NFO", "action": "SELL", "quantity": 15, "lot_size": 15}, {"symbol": "BANKNIFTY24MAR48000PE", "exchange": "NFO", "action": "SELL", "quantity": 15, "lot_size": 15}]'::jsonb,
    NOW() - INTERVAL '1 hour',
    NULL,
    4200.0,
    NULL,
    0.0,
    320.0,
    280.0,
    NOW() + INTERVAL '5 hours',
    'OPEN',
    NULL,
    '{"delta": -0.02, "gamma": -0.001, "theta": 85.3, "vega": -18.0}'::jsonb
),
(
    'b2c3d4e5-3333-4bbb-cccc-000000000003',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NULL,
    'long_put',
    'NIFTY',
    'NSE_FO',
    '[{"symbol": "NIFTY24MAR23400PE", "exchange": "NFO", "action": "BUY", "quantity": 50, "lot_size": 50}]'::jsonb,
    NOW() - INTERVAL '1 day',
    NOW() - INTERVAL '20 hours',
    8750.0,
    11200.0,
    2450.0,
    140.0,
    260.0,
    NOW() - INTERVAL '18 hours',
    'TARGET_HIT',
    'TARGET_HIT',
    '{"delta": -0.48, "gamma": 0.003, "theta": -38.0, "vega": 11.2}'::jsonb
);

-- ============================================================================
-- 5 Orders (mix of filled, pending)
-- ============================================================================
INSERT INTO orders (id, tenant_id, position_id, broker_order_id, symbol, exchange, action, quantity, lot_size, order_type, limit_price, fill_price, fill_time, status, error, created_at)
VALUES
(
    'c3d4e5f6-1111-4ccc-dddd-000000000001',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'b2c3d4e5-1111-4bbb-cccc-000000000001',
    'PAPER-20260314-001',
    'NIFTY24MAR23500CE',
    'NFO',
    'BUY',
    50,
    50,
    'MARKET',
    NULL,
    250.0,
    NOW() - INTERVAL '2 hours',
    'FILLED',
    NULL,
    NOW() - INTERVAL '2 hours'
),
(
    'c3d4e5f6-2222-4ccc-dddd-000000000002',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'b2c3d4e5-2222-4bbb-cccc-000000000002',
    'PAPER-20260314-002',
    'BANKNIFTY24MAR49000CE',
    'NFO',
    'SELL',
    15,
    15,
    'LIMIT',
    185.0,
    184.50,
    NOW() - INTERVAL '1 hour',
    'FILLED',
    NULL,
    NOW() - INTERVAL '1 hour'
),
(
    'c3d4e5f6-3333-4ccc-dddd-000000000003',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'b2c3d4e5-2222-4bbb-cccc-000000000002',
    'PAPER-20260314-003',
    'BANKNIFTY24MAR48000PE',
    'NFO',
    'SELL',
    15,
    15,
    'LIMIT',
    95.0,
    95.50,
    NOW() - INTERVAL '58 minutes',
    'FILLED',
    NULL,
    NOW() - INTERVAL '1 hour'
),
(
    'c3d4e5f6-4444-4ccc-dddd-000000000004',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'b2c3d4e5-3333-4bbb-cccc-000000000003',
    'PAPER-20260313-001',
    'NIFTY24MAR23400PE',
    'NFO',
    'BUY',
    50,
    50,
    'MARKET',
    NULL,
    175.0,
    NOW() - INTERVAL '1 day',
    'FILLED',
    NULL,
    NOW() - INTERVAL '1 day'
),
(
    'c3d4e5f6-5555-4ccc-dddd-000000000005',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NULL,
    NULL,
    'NIFTY24MAR23600CE',
    'NFO',
    'BUY',
    50,
    50,
    'LIMIT',
    180.0,
    NULL,
    NULL,
    'PENDING',
    NULL,
    NOW() - INTERVAL '10 minutes'
);

-- ============================================================================
-- 3 Trade Journal Entries (for the closed position + 2 historical)
-- ============================================================================
INSERT INTO trade_journal (id, tenant_id, position_id, trade_date, strategy, underlying, segment, entry_time, exit_time, exit_reason, entry_cost_inr, exit_value_inr, realised_pnl_inr, pnl_pct, was_in_plan, stop_loss_respected, time_stop_respected, override_count, discipline_score, pre_market_thesis, post_trade_notes, created_at, updated_at)
VALUES
(
    'd4e5f6a7-1111-4ddd-eeee-000000000001',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    'b2c3d4e5-3333-4bbb-cccc-000000000003',
    CURRENT_DATE - 1,
    'long_put',
    'NIFTY',
    'NSE_FO',
    NOW() - INTERVAL '1 day',
    NOW() - INTERVAL '20 hours',
    'TARGET_HIT',
    8750.0,
    11200.0,
    2450.0,
    28.0,
    TRUE,
    TRUE,
    TRUE,
    0,
    95.0,
    'Expecting NIFTY weakness on global cues. Plan to buy puts near 23400 strike.',
    'Clean execution. Target hit within 4 hours. RSI reversal signal was strong.',
    NOW() - INTERVAL '20 hours',
    NOW() - INTERVAL '20 hours'
),
(
    'd4e5f6a7-2222-4ddd-eeee-000000000002',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NULL,
    CURRENT_DATE - 3,
    'long_call',
    'BANKNIFTY',
    'NSE_FO',
    NOW() - INTERVAL '3 days',
    NOW() - INTERVAL '3 days' + INTERVAL '3 hours',
    'STOP_HIT',
    6200.0,
    4960.0,
    -1240.0,
    -20.0,
    TRUE,
    TRUE,
    TRUE,
    0,
    88.0,
    'BANKNIFTY showing strength. Buying calls at 48500 strike.',
    'Stopped out. Market reversed after initial move up. Stop loss worked as designed.',
    NOW() - INTERVAL '3 days' + INTERVAL '3 hours',
    NOW() - INTERVAL '3 days' + INTERVAL '3 hours'
),
(
    'd4e5f6a7-3333-4ddd-eeee-000000000003',
    '6105f2e9-dd72-4dce-8791-76e7a267443e',
    NULL,
    CURRENT_DATE - 5,
    'iron_condor',
    'NIFTY',
    'NSE_FO',
    NOW() - INTERVAL '5 days',
    NOW() - INTERVAL '5 days' + INTERVAL '6 hours',
    'TIME_STOP',
    3800.0,
    5100.0,
    1300.0,
    34.2,
    TRUE,
    TRUE,
    TRUE,
    0,
    92.0,
    'Range-bound day expected. IV Rank at 60. Setting up iron condor on NIFTY.',
    'Time stop triggered at EOD. Collected good premium. Discipline maintained.',
    NOW() - INTERVAL '5 days' + INTERVAL '6 hours',
    NOW() - INTERVAL '5 days' + INTERVAL '6 hours'
);
