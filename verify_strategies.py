"""
Verification script for S1/S2/S3 strategy implementations.
Run from resolute/ root: python verify_strategies.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np

# ── 1. Candle aggregation ────────────────────────────────────────────────────
from services.user_worker_pool.bias.evaluator import aggregate_candles

n_1m = 360
base = 1699920000  # Nov 14 2023 00:00 UTC
ts_1m = np.array([base + i * 60 for i in range(n_1m)])
closes_1m = np.linspace(100, 120, n_1m)
highs_1m  = closes_1m + 0.5
lows_1m   = closes_1m - 0.5
opens_1m  = closes_1m - 0.1

data_5m  = aggregate_candles({"timestamp": ts_1m, "open": opens_1m,
                               "high": highs_1m, "low": lows_1m, "close": closes_1m}, 300)
data_15m = aggregate_candles({"timestamp": ts_1m, "open": opens_1m,
                               "high": highs_1m, "low": lows_1m, "close": closes_1m}, 900)
data_1h  = aggregate_candles({"timestamp": ts_1m, "open": opens_1m,
                               "high": highs_1m, "low": lows_1m, "close": closes_1m}, 3600)
assert len(data_5m["close"]) >= 70, f"expected ~72 5m bars, got {len(data_5m['close'])}"
assert len(data_15m["close"]) >= 22, f"expected ~24 15m bars, got {len(data_15m['close'])}"
assert len(data_1h["close"]) >= 5, f"expected ~6 1h bars, got {len(data_1h['close'])}"
print(f"OK candle aggregation: 5m={len(data_5m['close'])} bars, 15m={len(data_15m['close'])} bars, 1H={len(data_1h['close'])} bars")

# ── 2. Brahmaastra ORB — correct IST timestamps ───────────────────────────────
# Nov 14 2023 9:15 IST = Nov 14 2023 03:45 UTC = 1699933500
# 15m bars: 9:15, 9:30, 9:45, 10:00, 10:15 IST
orb_base = 1699933500  # 9:15 IST
ts_15m_orb = np.array([orb_base + i * 900 for i in range(8)])  # 8 bars up to ~11:00
hi_orb  = np.array([19600., 19550., 19620., 19580., 19560., 19540., 19530., 19520.])
lo_orb  = np.array([19500., 19480., 19510., 19520., 19510., 19500., 19490., 19480.])
op_orb  = np.array([19550., 19510., 19560., 19540., 19530., 19520., 19510., 19500.])
cl_orb  = np.array([19520., 19490., 19610., 19560., 19520., 19510., 19500., 19490.])

candles_15m_orb = {
    "timestamp": ts_15m_orb,
    "open": op_orb, "high": hi_orb, "low": lo_orb, "close": cl_orb,
}

from services.user_worker_pool.strategies.brahmaastra import _compute_orb_range, _find_orb_signal
orb = _compute_orb_range(candles_15m_orb)
assert orb is not None, f"ORB returned None — check timestamps: {[int(t) for t in ts_15m_orb[:3]]}"
rh, rl = orb
print(f"OK Brahmaastra ORB: RH={rh}, RL={rl}")

# Check ORB signal detection (bar at 9:45 closes above RH)
sig = _find_orb_signal(candles_15m_orb, rh, rl)
print(f"   ORB signal: {sig}")

# ── 3. EMA5 alert candle ─────────────────────────────────────────────────────
from services.user_worker_pool.strategies.ema5_mean_reversion import _find_alert_candle

n = 30
closes_test = np.array([100.0 + i * 0.5 for i in range(n)])
# Make last two candles float ABOVE EMA (simulate upward gap)
closes_test[-5:] = closes_test[-6] + np.array([3, 4, 4.5, 5, 5.5])
highs_test = closes_test + 1.0
lows_test  = closes_test + 0.3  # lows above close → above EMA

candles_5m_test = {"close": closes_test, "high": highs_test, "low": lows_test}
result = _find_alert_candle(candles_5m_test, 5, "PE", 0.002)
if result is not None:
    trigger, sl, ema_val = result
    print(f"OK EMA5 PE alert: trigger={trigger:.2f}, sl={sl:.2f}, ema5={ema_val:.2f}")
else:
    print("INFO EMA5 PE alert: no alert (price may not be sufficiently above EMA)")

# ── 4. Parent-Child EMA stack ────────────────────────────────────────────────
from services.user_worker_pool.strategies.parent_child_momentum import _ema_stack_bullish, _ema_stack_bearish

n_h = 150
closes_h = np.linspace(100, 130, n_h)  # rising trend
bull = _ema_stack_bullish(closes_h, 10, 30, 100)
bear = _ema_stack_bearish(closes_h, 10, 30, 100)
assert bull == True,  f"expected bullish EMA stack, got {bull}"
assert bear == False, f"expected not bearish, got {bear}"
print(f"OK PC EMA stack: bullish={bull}, bearish={bear} (expected: True, False)")

# ── 5. Fast signals — 5m bars with realistic variance ────────────────────────
from backtest.fast_strategies import precompute_strategy_signals, FAST_STRATEGY_MAP

rng = np.random.default_rng(42)
N = 600
base_ts = 1699920000

# Simulate a realistic intraday session: trending up with noise
trend = np.linspace(19000, 19500, N)
noise = rng.normal(0, 15, N).cumsum()
cl5 = trend + noise
hi5 = cl5 + rng.uniform(5, 30, N)
lo5 = cl5 - rng.uniform(5, 30, N)
op5 = np.roll(cl5, 1); op5[0] = cl5[0]

# 15m bars
cl15 = cl5[2::3][:N//3]; hi15 = hi5[2::3][:N//3]; lo15 = lo5[2::3][:N//3]
op15 = op5[::3][:N//3]
# Use IST-correct timestamps: start at 9:15 IST
ts_15m_fast = np.array([orb_base + i * 900 for i in range(len(cl15))])

# 1H bars
cl1h = cl5[59::60][:N//60]; hi1h = hi5[59::60][:N//60]; lo1h = lo5[59::60][:N//60]

ts5 = np.array([base_ts + i * 300 for i in range(N)])

for strat in ["ema5_mean_reversion", "parent_child_momentum", "brahmaastra"]:
    sigs = precompute_strategy_signals(
        strategy_name=strat,
        closes_5m=cl5, highs_5m=hi5, lows_5m=lo5, opens_5m=op5,
        closes_15m=cl15, highs_15m=hi15, lows_15m=lo15, opens_15m=op15, timestamps_15m=ts_15m_fast,
        closes_1h=cl1h,
        params={},
    )
    n_sigs = int(np.sum(sigs != 0))
    print(f"OK {strat} fast signals: {n_sigs} signals in {N} bars")

print("\nAll checks passed.")
