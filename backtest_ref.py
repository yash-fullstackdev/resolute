"""Multi-Timeframe Scalping Backtester for TradeDash.

Architecture: 5m bias (direction) → 1m trigger (entry) → ATR-based exits.
Data: Always loads from local data/ folder (run download_candles.py first).

Usage:
  python backtest.py                    # backtest today
  python backtest.py --date 2026-03-10  # backtest a specific past date
  python backtest.py --date 2026-03-10 --date 2026-03-11  # multi-day
"""

import sys
import math
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from config import INSTRUMENTS
from download_candles import load_candles
from indicators import ema, atr_wilder, rsi_wilder, bollinger_bands, keltner_channels, vwap_with_bands, volume_ratio
from strategies import (
    TTMSqueezeStrategy, SupertrendStrategy,
    VWAPSupertrendStrategy, EMABreakdownStrategy, EMABiasFilter,
    SMCOrderBlockStrategy, EMA33Strategy
)
from session import (
    OPENING_DRIVE, MIDDAY_LULL, POWER_HOUR, CLOSING, CLOSED,
    get_current_session
)

IST = timezone(timedelta(hours=5, minutes=30))

# ═══════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════

SCALP_CONFIG = {
    'sl_atr_mult': 0.5,       # SL = 0.5 × ATR(14) on 5m
    'tp_atr_mult': 1.5,       # TP = 1.5 × ATR(14) on 5m
    'max_hold_candles': 20,    # Time-based exit after 20 × 1m candles  (was 10)
    'morning_trade_limit': 6,  # Max trades per instrument in morning session
    'afternoon_trade_limit': 3, # Max trades per instrument in afternoon session
    'slippage_pts': 0.5,       # Slippage per trade (index points)
    'brokerage_per_trade': 40, # ₹ per round trip for options
    # Session windows (minutes from midnight IST)
    'entry_start': 9 * 60 + 20,    # 09:20
    'morning_end': 11 * 60 + 30,   # 11:30
    'afternoon_start': 13 * 60,    # 13:00
    'entry_cutoff': 14 * 60 + 30,  # 14:30
    'force_exit': 15 * 60 + 15,    # 15:15
    # Bias consensus: how many 5m indicators must agree
    'min_bias_agreement': 2,       # out of 3 (EMA bias, Supertrend, Squeeze momentum)
    # Anti-whipsaw: cooldown after bias flip (in 1m candles)
    'bias_cooldown_candles': 10,   # 10 minutes cooldown after bias change
    # Gap day handling: use price action bias for first 30 mins
    'gap_price_action_duration': 30,  # minutes to use price action bias
    'gap_threshold_pct': 0.3,      # Gap > 0.3% triggers price action mode
    # SMC-specific parameters
    'smc_ob_sl_mode': 'ob_boundary',  # Use OB edge as SL instead of ATR
    'smc_sweep_cooldown': 5,   # Extra cooldown bars after sweep event
    'smc_fvg_tp_mode': True,   # Use next FVG midpoint as first TP target
    # EMA33 parameters
    'ema33_daily_profit_cap':  5000,    # ₹5000 daily rule — stop after this
    'ema33_grade_b_size_mult': 0.5,     # Grade B trades flagged (info only)
    'ema33_max_fires':         3,       # Max 3 trades/day (sniper approach)
    'ema33_use_own_sl':        True,    # Use OB boundary SL from signal dict
    # Risk controls
    'daily_loss_cap':          -80,     # Stop new entries once day's closed P&L < -80 pts
}

# Per-instrument SL caps (max SL in index points)
SL_CAPS = {
    'NIFTY 50': 20,
    'BANK NIFTY': 40,
}


# ═══════════════════════════════════════════════════
#  DATA LOADING (from local data/ folder)
# ═══════════════════════════════════════════════════
from data_feed import fetch_intraday

def fetch_candles(sec_id, name, date_str):
    """Load 1m and 5m candles from local data/ folder or fetch from API.

    Returns (data_1m, data_5m) dicts or (None, None) if not found.
    """
    print(f"  Loading 1m candles for {name} ({date_str})...", end=" ")
    data_1m = load_candles(name, date_str, "1")
    if data_1m and 'close' in data_1m:
        print(f"✓ {len(data_1m['close'])} candles (local)")
    else:
        print(f"fetching from API...", end=" ")
        api_data = fetch_intraday(sec_id, "1", date_str)
        if api_data and 'close' in api_data:
            from download_candles import normalize_data
            data_1m = normalize_data(api_data)
            print(f"✓ {len(data_1m['close'])} candles (API)")
        else:
            print(f"✗ not found via API either")
            return None, None

    print(f"  Loading 5m candles for {name} ({date_str})...", end=" ")
    data_5m = load_candles(name, date_str, "5")
    if data_5m and 'close' in data_5m:
        print(f"✓ {len(data_5m['close'])} candles (local)")
    else:
        print(f"fetching from API...", end=" ")
        api_data = fetch_intraday(sec_id, "5", date_str)
        if api_data and 'close' in api_data:
            from download_candles import normalize_data
            data_5m = normalize_data(api_data)
            print(f"✓ {len(data_5m['close'])} candles (API)")
        else:
            print(f"✗ not found via API either")
            return None, None

    return data_1m, data_5m


def slice_data(data, end_idx):
    """Return data dict sliced to [:end_idx] for all lists."""
    return {k: v[:end_idx] if isinstance(v, list) else v for k, v in data.items()}


# ═══════════════════════════════════════════════════
#  5-MINUTE BIAS ENGINE
# ═══════════════════════════════════════════════════

_ema_filter    = EMABiasFilter(short_period=3, long_period=11)
_supertrend    = SupertrendStrategy(period=10, multiplier=3.0)
_ttm           = TTMSqueezeStrategy()

# 15m macro trend — same EMA(3/11) logic, applied to 15m bars
_ema_filter_15m = EMABiasFilter(short_period=9, long_period=15)


def get_5m_bias(data_5m_slice):
    """Compute directional bias from 5m data.

    Uses 3 indicators, requires at least 2 to agree:
      1. EMA 9/21 bias (BUY if 9 > 21, SELL if 9 < 21)
      2. Supertrend direction (+1 = BUY, -1 = SELL)
      3. TTM Squeeze momentum (positive = BUY, negative = SELL)

    SMC runs independently with its own bias logic.

    Returns: ('BUY', details) | ('SELL', details) | (None, details)
    """
    votes = {'BUY': 0, 'SELL': 0}
    details = {}

    # 1. EMA Bias
    ema_bias = _ema_filter.get_bias(data_5m_slice)
    details['ema_bias'] = ema_bias or 'FLAT'
    if ema_bias == 'BUY':
        votes['BUY'] += 1
    elif ema_bias == 'SELL':
        votes['SELL'] += 1

    # 2. Supertrend Direction
    st_state = _supertrend.get_state(data_5m_slice)
    st_dir = st_state['direction']
    details['supertrend'] = 'BUY' if st_dir == 1 else ('SELL' if st_dir == -1 else 'FLAT')
    if st_dir == 1:
        votes['BUY'] += 1
    elif st_dir == -1:
        votes['SELL'] += 1

    # 3. TTM Squeeze Momentum
    sq_state = _ttm.get_squeeze_state(data_5m_slice)
    mom = sq_state['momentum']
    details['squeeze_mom'] = mom
    details['squeezing'] = sq_state['squeezing']
    if mom > 0:
        votes['BUY'] += 1
    elif mom < 0:
        votes['SELL'] += 1

    # 4. EMA33 Zone — adds vote only when RSI is clearly outside 40-60
    _ema33_bias = EMA33Strategy()
    ema33_state = _ema33_bias.get_state(data_5m_slice)
    ema33_zone  = ema33_state.get('zone', 'NO_TRADE')
    details['ema33_zone'] = ema33_zone
    if ema33_zone == 'BULL':
        votes['BUY']  += 1
    elif ema33_zone == 'BEAR':
        votes['SELL'] += 1
    # NO_TRADE zone: adds 0 votes — soft veto on choppy RSI conditions

    # Consensus (2 out of 4)
    min_agree = 2
    if votes['BUY'] >= min_agree:
        return 'BUY', details
    elif votes['SELL'] >= min_agree:
        return 'SELL', details
    return None, details


def get_15m_trend(data_15m_slice):
    """Macro trend direction from 15m EMA(3/11) crossover.

    Returns 'BUY', 'SELL', or None (insufficient data).
    Needs ≥12 bars (~180 min from open) before it activates — morning
    session will receive None and the filter is skipped gracefully.
    """
    if not data_15m_slice or len(data_15m_slice.get('close', [])) < 13:
        return None
    return _ema_filter_15m.get_bias(data_15m_slice)


def get_price_action_bias(data_1m_slice, gap_direction):
    """Get bias from pure price action for opening drive on gap days.
    
    Logic: On gap up, assume BUY bias until price breaks below VWAP.
           On gap down, assume SELL bias until price breaks above VWAP.
    
    Returns: 'BUY' | 'SELL' | None
    """
    if not data_1m_slice or 'close' not in data_1m_slice:
        return None
    
    closes = data_1m_slice['close']
    highs = data_1m_slice.get('high', [])
    lows = data_1m_slice.get('low', [])
    volumes = data_1m_slice.get('volume', [])
    
    if len(closes) < 20 or not volumes or len(volumes) < 20:
        return None
    
    vwap_data = vwap_with_bands(highs, lows, closes, volumes)
    if not vwap_data or not vwap_data['vwap']:
        return None
    
    current_price = closes[-1]
    vwap = vwap_data['vwap'][-1]
    
    if gap_direction == 'BUY':
        # Gap up: BUY bias unless price < VWAP
        return 'BUY' if current_price >= vwap else None
    elif gap_direction == 'SELL':
        # Gap down: SELL bias unless price > VWAP
        return 'SELL' if current_price <= vwap else None
    
    return None


# ═══════════════════════════════════════════════════
#  1-MINUTE ENTRY TRIGGERS
# ═══════════════════════════════════════════════════

def check_1m_entries(data_1m_slice, data_5m_slice, bias, no_bias=False, t_minutes=0,
                     trend_15m=None):
    """Check for scalping entry signals on 1m, optionally filtered by 5m bias.

    Multi-timeframe hierarchy:
      15m trend  → macro direction (EMA 3/11 on 15m bars built from 1m data)
      5m bias    → medium direction (4-indicator consensus)
      1m trigger → entry (strategy-specific)

    A signal is accepted only when it aligns with ALL available timeframes.
    If trend_15m is None (not enough bars yet — typical before ~12:15 IST),
    the 15m filter is skipped gracefully.

    Concurrent (independent): SMC_OB, EMA33_OB
    Concurrent (bias-filtered): TTM_Squeeze
    Non-concurrent (bias-filtered): VWAP_ST_Combo

    Session routing (data-driven over 92 days):
      SMC_OB:        afternoon only  (morning PF 0.84 = -110 pts drag)
      VWAP_ST_Combo: morning only    (afternoon PF 0.74 = -170 pts drag)

    If no_bias=True, 5m bias filter is skipped for all strategies.
    Returns list of signal dicts (may be empty).
    """
    signals = []
    is_morning   = t_minutes < 11 * 60 + 30   # before 11:30
    is_afternoon = t_minutes >= 13 * 60        # 13:00 onwards

    # SMC — afternoon only (morning: PF 0.84, -110 pts over 92 days)
    if is_afternoon:
        try:
            sig = SMCOrderBlockStrategy().analyze(data_5m=data_5m_slice, data_1m=data_1m_slice)
            if sig:
                signals.append(sig)
        except Exception:
            pass

    # TTM_Squeeze — bias-filtered, concurrent
    if bias or no_bias:
        try:
            sig = TTMSqueezeStrategy().analyze(data_5m=data_5m_slice, data_1m=data_1m_slice)
            if sig and (no_bias or sig.get('signal_type') == bias):
                vols = data_1m_slice.get('volume', [])
                vol_ok = volume_ratio(vols, period=20) >= 1.15 if len(vols) >= 20 else True
                if vol_ok:
                    signals.append(sig)
        except Exception:
            pass

    # EMA33 — independent, no bias filter, concurrent
    try:
        sig = EMA33Strategy().analyze(data_5m=data_5m_slice, data_1m=data_1m_slice)
        if sig:
            signals.append(sig)
    except Exception:
        pass

    # VWAP_ST_Combo — morning only (afternoon: PF 0.74, -170 pts over 92 days)
    if (bias or no_bias) and is_morning:
        try:
            sig = VWAPSupertrendStrategy().analyze(data_5m=data_5m_slice, data_1m=data_1m_slice)
            if sig and (no_bias or sig.get('signal_type') == bias):
                signals.append(sig)
        except Exception:
            pass

    # ── 15m macro trend filter ──
    # Applied last so it acts as a universal gate across all strategies.
    # If trend_15m is None (insufficient bars, typically before ~12:15 IST)
    # we pass all signals through unchanged — morning session is unaffected.
    if trend_15m and not no_bias:
        signals = [s for s in signals if s.get('signal_type') == trend_15m]

    return signals


def _rsi_vwap_scalp(data_1m_slice, bias):
    """Custom scalping trigger: RSI extreme + price at VWAP band.

    BUY:  RSI < 30 and price near/below VWAP lower band
    SELL: RSI > 70 and price near/above VWAP upper band
    """
    closes = data_1m_slice.get('close', [])
    if len(closes) < 20:
        return None

    rsi_vals = rsi_wilder(closes, 14)
    if not rsi_vals:
        return None
    rsi = rsi_vals[-1]

    highs = data_1m_slice.get('high', [])
    lows = data_1m_slice.get('low', [])
    volumes = data_1m_slice.get('volume', [])
    if not volumes or len(volumes) < 20:
        return None

    vwap_data = vwap_with_bands(highs, lows, closes, volumes)
    if not vwap_data or not vwap_data['vwap']:
        return None

    curr_close = closes[-1]
    vwap_val = vwap_data['vwap'][-1]
    lower_1 = vwap_data['lower_1'][-1]
    upper_1 = vwap_data['upper_1'][-1]

    if bias == 'BUY' and rsi < 30 and curr_close <= lower_1:
        return {
            'signal_type': 'BUY', 'price': curr_close,
            'strategy': 'RSI_VWAP_Scalp', 'rsi': round(rsi, 1),
            'vwap': round(vwap_val, 2), 'confidence': 0.7,
            'timestamp': data_1m_slice.get('timestamp', [0])[-1],
        }
    elif bias == 'SELL' and rsi > 70 and curr_close >= upper_1:
        return {
            'signal_type': 'SELL', 'price': curr_close,
            'strategy': 'RSI_VWAP_Scalp', 'rsi': round(rsi, 1),
            'vwap': round(vwap_val, 2), 'confidence': 0.7,
            'timestamp': data_1m_slice.get('timestamp', [0])[-1],
        }
    return None


# ═══════════════════════════════════════════════════
#  TRADE SIMULATOR
# ═══════════════════════════════════════════════════

def ts_to_ist_time(ts):
    """Convert unix timestamp to IST minutes-from-midnight."""
    dt = datetime.fromtimestamp(ts, tz=IST)
    return dt.hour * 60 + dt.minute


def ts_to_ist_str(ts):
    """Convert unix timestamp to IST time string."""
    dt = datetime.fromtimestamp(ts, tz=IST)
    return dt.strftime('%H:%M')


def is_entry_allowed(ts):
    """Check if we're in an allowed scalping session window."""
    t = ts_to_ist_time(ts)
    cfg = SCALP_CONFIG
    morning_ok = cfg['entry_start'] <= t <= cfg['morning_end']
    afternoon_ok = cfg['afternoon_start'] <= t <= cfg['entry_cutoff']
    return morning_ok or afternoon_ok


def is_force_exit_time(ts):
    """Check if we've hit the force exit time (15:15)."""
    t = ts_to_ist_time(ts)
    return t >= SCALP_CONFIG['force_exit']


def compute_atr_from_5m(data_5m_slice):
    """Get latest ATR(14) value from 5m data."""
    if not data_5m_slice or 'close' not in data_5m_slice:
        return None
    highs = data_5m_slice['high']
    lows = data_5m_slice['low']
    closes = data_5m_slice['close']
    if len(closes) < 16:
        return None
    atr_vals = atr_wilder(highs, lows, closes, 14)
    if not atr_vals:
        return None
    return atr_vals[-1]


def find_5m_index_for_1m_ts(ts_1m, timestamps_5m):
    """Find how many 5m candles have CLOSED before this 1m timestamp."""
    count = 0
    for ts_5m in timestamps_5m:
        if ts_5m + 300 <= ts_1m:
            count += 1
        else:
            break
    return count


def find_15m_index_for_1m_ts(ts_1m, timestamps_15m):
    """Find how many 15m candles have CLOSED before this 1m timestamp.

    A 15m bar covers [ts_15m, ts_15m+900). It is fully closed when
    ts_15m + 900 <= ts_1m.
    """
    count = 0
    for ts_15m in timestamps_15m:
        if ts_15m + 900 <= ts_1m:
            count += 1
        else:
            break
    return count


def build_15m_data(data_1m):
    """Aggregate 1m candles into 15m OHLCV bars.

    Uses floor-division on unix timestamps to group 1m bars into
    15-minute buckets aligned to the clock (e.g. 09:15, 09:30 …).
    The last (currently-forming) bar is NOT included to avoid any
    future-data leak — only fully-closed 15m bars are emitted.

    Returns a data dict with the same keys as data_1m.
    """
    timestamps = data_1m.get('timestamp', [])
    result = {'timestamp': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []}
    if not timestamps:
        return result

    has_vol = 'volume' in data_1m
    cur_period = None
    o_buf, h_buf, l_buf, c_buf, v_buf = [], [], [], [], []

    for i, ts in enumerate(timestamps):
        period = (ts // 900) * 900  # floor to 15m boundary

        if period != cur_period:
            # Flush completed bar (not the very first iteration)
            if c_buf and cur_period is not None:
                result['timestamp'].append(cur_period)
                result['open'].append(o_buf[0])
                result['high'].append(max(h_buf))
                result['low'].append(min(l_buf))
                result['close'].append(c_buf[-1])
                result['volume'].append(sum(v_buf) if v_buf else 0)
            cur_period = period
            o_buf = [data_1m['open'][i]]
            h_buf = [data_1m['high'][i]]
            l_buf = [data_1m['low'][i]]
            c_buf = [data_1m['close'][i]]
            v_buf = [data_1m['volume'][i]] if has_vol else []
        else:
            h_buf.append(data_1m['high'][i])
            l_buf.append(data_1m['low'][i])
            c_buf.append(data_1m['close'][i])
            if has_vol:
                v_buf.append(data_1m['volume'][i])

    # The last partial bar is intentionally NOT flushed (no future leak)
    return result


def build_partial_5m_bar(data_1m, end_idx, timestamps_5m):
    """Build the current (incomplete) 5m bar from available 1m candles.

    At 1m bar index `end_idx`, we find which 5m period is currently forming
    and aggregate all 1m bars within that period into a single OHLCV bar.
    This is exactly what the live REST API returns — no future data.

    Returns a dict with single-element lists {open:[x], high:[y], ...} or None.
    """
    ts_1m = data_1m['timestamp'][end_idx] if end_idx < len(data_1m['timestamp']) else 0
    if not ts_1m or not timestamps_5m:
        return None

    # Find the 5m period that is currently forming (opened but not closed)
    current_5m_open = None
    for ts_5m in timestamps_5m:
        if ts_5m <= ts_1m < ts_5m + 300:
            current_5m_open = ts_5m
            break

    if current_5m_open is None:
        return None

    # Gather 1m bars within this 5m period, up to and including end_idx
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for j in range(end_idx + 1):
        bar_ts = data_1m['timestamp'][j]
        if current_5m_open <= bar_ts < current_5m_open + 300:
            opens.append(data_1m['open'][j])
            highs.append(data_1m['high'][j])
            lows.append(data_1m['low'][j])
            closes.append(data_1m['close'][j])
            if 'volume' in data_1m:
                volumes.append(data_1m['volume'][j])

    if not closes:
        return None

    bar = {
        'timestamp': [current_5m_open],
        'open': [opens[0]],
        'high': [max(highs)],
        'low': [min(lows)],
        'close': [closes[-1]],
    }
    if volumes:
        bar['volume'] = [sum(volumes)]
    return bar


def append_partial_bar(data_5m_slice, partial_bar):
    """Append a partial 5m bar to a sliced 5m dataset."""
    if not partial_bar:
        return data_5m_slice
    result = {}
    for k in data_5m_slice:
        if isinstance(data_5m_slice[k], list) and k in partial_bar:
            result[k] = data_5m_slice[k] + partial_bar[k]
        else:
            result[k] = data_5m_slice[k]
    return result


CONCURRENT_STRATEGIES = {'SMC_OB', 'TTM_Squeeze', 'EMA33_OB'}


def run_backtest(sec_id, name, data_1m, data_5m, no_bias=False):
    """Walk through 1m candles, apply multi-TF logic, simulate trades.

    SMC_OB and TTM_Squeeze can open concurrently even when another trade is open.
    All other strategies require no open trade.

    Returns list of trade dicts.
    """
    trades = []
    open_trade = None          # single slot for non-concurrent strategies
    open_concurrent = []       # concurrent slots for SMC_OB / TTM_Squeeze
    morning_trades = 0
    afternoon_trades = 0
    cfg = SCALP_CONFIG

    closes_1m = data_1m['close']
    highs_1m = data_1m['high']
    lows_1m = data_1m['low']
    timestamps_1m = data_1m.get('timestamp', [])
    timestamps_5m = data_5m.get('timestamp', [])

    # Build 15m dataset upfront from 1m candles (no API call needed)
    data_15m = build_15m_data(data_1m)
    timestamps_15m = data_15m.get('timestamp', [])

    n_1m = len(closes_1m)
    if n_1m == 0:
        return trades

    last_bias = None
    last_bias_details = None
    last_bias_change_bar = -999  # bar index when bias last changed (for cooldown)
    has_flipped_today = False
    is_large_gap = False
    gap_pct = 0.0  # Store gap percentage
    gap_direction = None  # 'BUY' for gap up, 'SELL' for gap down
    initial_morning_bias = None
    gap_start_time = 0  # Track when gap mode started
    daily_closed_pnl = 0.0   # running P&L of closed trades today
    _last_day = None          # date string for daily reset

    print(f"\n  Walking {n_1m} × 1m candles for {name}...")
    print(f"  {'─' * 60}")

    for i in range(1, n_1m):
        ts = timestamps_1m[i] if i < len(timestamps_1m) else 0
        price = closes_1m[i]
        high = highs_1m[i]
        low = lows_1m[i]
        t_minutes = ts_to_ist_time(ts) if ts else 0

        # Reset daily P&L tracker at day boundary
        candle_date = datetime.fromtimestamp(ts, tz=IST).strftime('%Y-%m-%d') if ts else ''
        if candle_date != _last_day:
            _last_day = candle_date
            daily_closed_pnl = 0.0

        # ── Determine current 5m context ──
        # Use only CLOSED 5m bars + a partial bar built from 1m data (no future leak).
        n_5m_bars = find_5m_index_for_1m_ts(ts, timestamps_5m) if ts else 0
        if n_5m_bars >= 5:
            data_5m_slice = slice_data(data_5m, n_5m_bars)
            # Append the currently-forming 5m bar reconstructed from 1m candles
            partial = build_partial_5m_bar(data_1m, i, timestamps_5m)
            data_5m_slice = append_partial_bar(data_5m_slice, partial)

            # ── 15m macro trend (built from 1m, no extra data needed) ──
            n_15m_bars = find_15m_index_for_1m_ts(ts, timestamps_15m) if ts else 0
            trend_15m = get_15m_trend(slice_data(data_15m, n_15m_bars)) if n_15m_bars >= 13 else None

            # --- OVERNIGHT GAP CHECK ---
            if i == 1:
                if len(data_5m_slice['close']) > 2:
                    yest_close = data_5m_slice['close'][-2]
                    today_open = data_5m_slice['open'][-1]
                    if yest_close > 0:
                        gap_pct = (today_open - yest_close) / yest_close * 100
                
                is_large_gap = abs(gap_pct) > cfg['gap_threshold_pct']
                if is_large_gap:
                    gap_direction = 'BUY' if gap_pct > 0 else 'SELL'
                    gap_start_time = ts
                    print(f"  [GAP DETECTED] {gap_pct:+.2f}% gap. Using price action bias for first {cfg['gap_price_action_duration']} mins.")

            # --- ADAPTIVE BIAS CALCULATION ---
            # On gap days, use price action for first 30 mins, then switch to 5m consensus
            minutes_since_gap = (ts - gap_start_time) / 60 if gap_start_time > 0 else 999
            use_price_action = is_large_gap and minutes_since_gap < cfg['gap_price_action_duration']
            
            if use_price_action:
                data_1m_slice = slice_data(data_1m, i + 1)
                bias = get_price_action_bias(data_1m_slice, gap_direction)
                bias_details = {'source': 'price_action', 'gap_direction': gap_direction, 'vwap_based': True}
            else:
                bias, bias_details = get_5m_bias(data_5m_slice)
            
            if bias != last_bias:
                source_label = "[Price Action]" if use_price_action else "[5m Consensus]"
                if use_price_action:
                    print(f"  {ts_to_ist_str(ts)} │ {source_label} BIAS: {bias} (gap {gap_direction}, price vs VWAP)")
                else:
                    print(f"  {ts_to_ist_str(ts)} │ {source_label} BIAS changed: {last_bias} → {bias}  "
                          f"(EMA:{bias_details.get('ema_bias', 'N/A')} "
                          f"ST:{bias_details.get('supertrend', 'N/A')} "
                          f"Mom:{bias_details.get('squeeze_mom', 'N/A')}) "
                          f"[15m:{trend_15m or 'wait'}]")
                
                # Track cooldown: if this is a real flip (not initial), mark bar
                if last_bias is not None and bias is not None:
                    last_bias_change_bar = i
                    has_flipped_today = True
                
                # Set initial bias to track the first flip
                if initial_morning_bias is None and bias is not None:
                    initial_morning_bias = bias

                last_bias = bias
                last_bias_details = bias_details
        else:
            data_5m_slice = None
            bias = None
            trend_15m = None

        # ── Manage open trade ──
        def _check_exit(ot):
            """Return (exit_reason, exit_price) for a trade dict, or (None, None)."""
            candles_held = i - ot['entry_bar']
            hit_sl = False
            hit_tp = False
            if ot['direction'] == 'BUY':
                hit_sl = low <= ot['sl']
                hit_tp = high >= ot['tp']
            else:
                hit_sl = high >= ot['sl']
                hit_tp = low <= ot['tp']

            if hit_tp and hit_sl:
                return 'SL_HIT', ot['sl']
            elif hit_tp:
                return 'TP_HIT', ot['tp']
            elif hit_sl:
                return 'SL_HIT', ot['sl']
            elif candles_held >= cfg['max_hold_candles']:
                return 'TIME_EXIT', price
            elif is_force_exit_time(ts):
                return 'FORCE_EXIT_3:15', price
            return None, None

        def _close_trade(ot, exit_reason, exit_price):
            direction = ot['direction']
            raw_pnl = (exit_price - ot['entry_price']) if direction == 'BUY' \
                else (ot['entry_price'] - exit_price)
            pnl = raw_pnl - cfg['slippage_pts']
            record = {
                'instrument': name,
                'direction': direction,
                'strategy': ot['strategy'],
                'entry_time': ts_to_ist_str(ot['entry_ts']),
                'exit_time': ts_to_ist_str(ts),
                'entry_price': round(ot['entry_price'], 2),
                'exit_price': round(exit_price, 2),
                'sl': round(ot['sl'], 2),
                'tp': round(ot['tp'], 2),
                'pnl_pts': round(pnl, 2),
                'exit_reason': exit_reason,
                'hold_candles': i - ot['entry_bar'],
                'bias_at_entry': ot['bias'],
                'atr_at_entry': round(ot['atr'], 2),
                'grade': ot.get('grade', 'A'),   # EMA33 grade logged for analysis
                'date': ot.get('date', ''),
            }
            trades.append(record)
            icon = '✅' if pnl > 0 else '❌'
            print(f"  {ts_to_ist_str(ts)} │ {icon} EXIT {direction} @ {exit_price:.2f} "
                  f"({exit_reason}) P&L={pnl:+.2f} pts [{ot['strategy']}]")
            return pnl

        # Check main (non-concurrent) trade
        if open_trade:
            exit_reason, exit_price = _check_exit(open_trade)
            if exit_reason:
                daily_closed_pnl += _close_trade(open_trade, exit_reason, exit_price)
                open_trade = None

        # Check concurrent trades (SMC_OB / TTM_Squeeze)
        still_open = []
        for ot in open_concurrent:
            exit_reason, exit_price = _check_exit(ot)
            if exit_reason:
                daily_closed_pnl += _close_trade(ot, exit_reason, exit_price)
            else:
                still_open.append(ot)
        open_concurrent = still_open

        # ── Check for new entry ──
        # Session-weighted trade limit check
        t_minutes = ts_to_ist_time(ts) if ts else 0
        is_morning = cfg['entry_start'] <= t_minutes < cfg['morning_end']
        session_limit = cfg['morning_trade_limit'] if is_morning else cfg['afternoon_trade_limit']
        session_count = morning_trades if is_morning else afternoon_trades

        # Bias cooldown: skip entries within N candles of a bias flip
        in_cooldown = (i - last_bias_change_bar) < cfg['bias_cooldown_candles']

        daily_capped = daily_closed_pnl < cfg.get('daily_loss_cap', float('-inf'))

        if session_count < session_limit and not in_cooldown and not daily_capped:
            # No more gap filter blocking — we use 1m EMA bias on gap days instead
            if ts and is_entry_allowed(ts) and (bias or no_bias) and data_5m_slice:
                data_1m_slice = slice_data(data_1m, i + 1)
                sigs = check_1m_entries(data_1m_slice, data_5m_slice, bias, no_bias=no_bias,
                                        t_minutes=t_minutes, trend_15m=trend_15m)

                for sig in sigs:
                    strategy_name = sig.get('strategy', 'Unknown')
                    is_concurrent = strategy_name in CONCURRENT_STRATEGIES

                    # Non-concurrent strategies: skip if any trade is open
                    # Concurrent strategies: skip only if same strategy already has an open slot
                    already_open_concurrent = any(
                        ot['strategy'] == strategy_name for ot in open_concurrent
                    )
                    can_enter = (
                        is_concurrent and not already_open_concurrent
                    ) or (
                        not is_concurrent and open_trade is None
                    )

                    if can_enter:
                        atr = compute_atr_from_5m(data_5m_slice)
                        if atr and atr > 0:
                            entry_price = price + cfg['slippage_pts'] if sig['signal_type'] == 'BUY' \
                                else price - cfg['slippage_pts']
                            
                            # Dynamic SL Caps: Wider in the morning (before 10:30)
                            if t_minutes < 1030:
                                dynamic_caps = {'NIFTY 50': 40, 'BANK NIFTY': 80}
                            else:
                                dynamic_caps = SL_CAPS

                            # EMA33-specific SL/TP using pullback candle boundary
                            if sig.get('strategy') == 'EMA33_OB' and cfg.get('ema33_use_own_sl') and sig.get('sl'):
                                sl = sig['sl']
                                tp = sig['tp1']   # T1 = 1x ATR (conservative for backtest)
                                # Note: Grade B trades fire at half conviction but same size in backtest
                                # To enforce half-size for Grade B, add position_mult to trade record

                            # SMC-specific SL/TP using OB boundaries
                            elif sig.get('strategy') == 'SMC_OB' and cfg['smc_ob_sl_mode'] == 'ob_boundary':
                                if sig['signal_type'] == 'BUY':
                                    sl_dist = min(entry_price - sig['ob_btm'] + 0.5 * atr, 
                                                dynamic_caps.get(name, 999))
                                    tp_dist = cfg['tp_atr_mult'] * atr
                                    sl = entry_price - sl_dist
                                    tp = entry_price + tp_dist
                                else:
                                    sl_dist = min(sig['ob_top'] - entry_price + 0.5 * atr, 
                                                dynamic_caps.get(name, 999))
                                    tp_dist = cfg['tp_atr_mult'] * atr
                                    sl = entry_price + sl_dist
                                    tp = entry_price - tp_dist
                            else:
                                # Standard ATR-based SL/TP
                                if sig['signal_type'] == 'BUY':
                                    sl_dist = min(cfg['sl_atr_mult'] * atr, dynamic_caps.get(name, 999))
                                    tp_dist = cfg['tp_atr_mult'] * atr
                                    sl = entry_price - sl_dist
                                    tp = entry_price + tp_dist
                                else:
                                    sl_dist = min(cfg['sl_atr_mult'] * atr, dynamic_caps.get(name, 999))
                                    tp_dist = cfg['tp_atr_mult'] * atr
                                    sl = entry_price + sl_dist
                                    tp = entry_price - tp_dist

                            new_trade = {
                                'direction': sig['signal_type'],
                                'entry_price': entry_price,
                                'sl': sl,
                                'tp': tp,
                                'entry_bar': i,
                                'entry_ts': ts,
                                'strategy': strategy_name,
                                'atr': atr,
                                'bias': bias,
                                'grade': sig.get('grade', 'A'),   # EMA33 grade (A/B)
                                'date': datetime.fromtimestamp(ts, tz=IST).strftime('%Y-%m-%d'),
                            }

                            if is_concurrent:
                                open_concurrent.append(new_trade)
                            else:
                                open_trade = new_trade

                            if is_morning:
                                morning_trades += 1
                            else:
                                afternoon_trades += 1

                            concurrent_tag = ' [concurrent]' if is_concurrent else ''
                            grade_tag = f" [Grade {sig['grade']}]" if sig.get('grade') else ''
                            print(f"  {ts_to_ist_str(ts)} │ 🎯 ENTRY {sig['signal_type']} @ {entry_price:.2f} "
                                  f"SL={sl:.2f} TP={tp:.2f} ATR={atr:.2f} [{strategy_name}]{grade_tag}{concurrent_tag}")

    # Force-close any remaining trades at end of data
    remaining = ([open_trade] if open_trade else []) + open_concurrent
    for open_trade in remaining:
        last_price = closes_1m[-1]
        direction = open_trade['direction']
        if direction == 'BUY':
            pnl = last_price - open_trade['entry_price'] - cfg['slippage_pts']
        else:
            pnl = open_trade['entry_price'] - last_price - cfg['slippage_pts']

        trade_record = {
            'instrument': name,
            'direction': direction,
            'strategy': open_trade['strategy'],
            'entry_time': ts_to_ist_str(open_trade['entry_ts']),
            'exit_time': ts_to_ist_str(timestamps_1m[-1]) if timestamps_1m else 'EOD',
            'entry_price': round(open_trade['entry_price'], 2),
            'exit_price': round(last_price, 2),
            'sl': round(open_trade['sl'], 2),
            'tp': round(open_trade['tp'], 2),
            'pnl_pts': round(pnl, 2),
            'exit_reason': 'EOD_CLOSE',
            'hold_candles': n_1m - 1 - open_trade['entry_bar'],
            'bias_at_entry': open_trade['bias'],
            'atr_at_entry': round(open_trade['atr'], 2),
            'date': open_trade.get('date', ''),
        }
        trades.append(trade_record)
        icon = '✅' if pnl > 0 else '❌'
        print(f"  EOD   │ {icon} FORCE EXIT {direction} @ {last_price:.2f} P&L={pnl:+.2f} pts")

    return trades


# ═══════════════════════════════════════════════════
#  RESULTS & ANALYTICS
# ═══════════════════════════════════════════════════

def print_trade_log(trades):
    """Print a formatted per-trade table."""
    if not trades:
        print("\n  No trades executed.")
        return

    has_dates = any(t.get('date') for t in trades)
    multi_day = len(set(t.get('date', '') for t in trades)) > 1

    if multi_day:
        print(f"\n{'═' * 150}")
        print(f"  {'#':>3} │ {'Date':<11} │ {'Time':>5} │ {'Inst':<8} │ {'Dir':<4} │ {'Strategy':<15} │ "
              f"{'Entry':>8} │ {'Exit':>8} │ {'SL':>6} │ {'TP':>6} │ {'RR':>6} │ {'P&L':>8} │ {'Reason':<12} │ {'Hold'}")
        print(f"{'─' * 150}")
    else:
        print(f"\n{'═' * 135}")
        print(f"  {'#':>3} │ {'Inst':<8} │ {'Dir':<4} │ {'Strategy':<15} │ {'Time':>5} │ "
              f"{'Entry':>8} │ {'Exit':>8} │ {'SL':>6} │ {'TP':>6} │ {'RR':>6} │ {'P&L':>8} │ {'Reason':<12} │ {'Hold'}")
        print(f"{'─' * 135}")

    for idx, t in enumerate(trades, 1):
        pnl_str = f"{t['pnl_pts']:+.2f}"
        pnl_color = '\033[92m' if t['pnl_pts'] > 0 else '\033[91m'
        reset = '\033[0m'
        
        # Calculate SL and TP in points
        if t['direction'] == 'BUY':
            sl_pts = t['entry_price'] - t['sl']
            tp_pts = t['tp'] - t['entry_price']
        else:
            sl_pts = t['sl'] - t['entry_price']
            tp_pts = t['entry_price'] - t['tp']
        
        rr_ratio = f"{tp_pts/sl_pts:.1f}" if sl_pts > 0 else "N/A"
        
        # Shorten instrument name
        inst_short = 'NIFTY' if 'NIFTY 50' in t['instrument'] else 'BNIFTY'

        if multi_day:
            print(f"  {idx:>3} │ {t.get('date', ''):<11} │ {t['entry_time']:>5} │ {inst_short:<8} │ {t['direction']:<4} │ {t['strategy']:<15} │ "
                  f"{t['entry_price']:>8.2f} │ {t['exit_price']:>8.2f} │ {sl_pts:>6.1f} │ {tp_pts:>6.1f} │ {rr_ratio:>6} │ "
                  f"{pnl_color}{pnl_str:>8}{reset} │ {t['exit_reason']:<12} │ {t['hold_candles']}m")
        else:
            print(f"  {idx:>3} │ {inst_short:<8} │ {t['direction']:<4} │ {t['strategy']:<15} │ {t['entry_time']:>5} │ "
                  f"{t['entry_price']:>8.2f} │ {t['exit_price']:>8.2f} │ {sl_pts:>6.1f} │ {tp_pts:>6.1f} │ {rr_ratio:>6} │ "
                  f"{pnl_color}{pnl_str:>8}{reset} │ {t['exit_reason']:<12} │ {t['hold_candles']}m")

    print(f"{'═' * (150 if multi_day else 135)}")


def print_summary(trades):
    """Print overall backtest statistics."""
    if not trades:
        return

    total_pnl = sum(t['pnl_pts'] for t in trades)
    winners = [t for t in trades if t['pnl_pts'] > 0]
    losers = [t for t in trades if t['pnl_pts'] <= 0]
    win_rate = len(winners) / len(trades) * 100 if trades else 0

    avg_win = sum(t['pnl_pts'] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t['pnl_pts'] for t in losers) / len(losers) if losers else 0
    gross_profit = sum(t['pnl_pts'] for t in winners)
    gross_loss = abs(sum(t['pnl_pts'] for t in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Max consecutive losses
    max_consec_loss = 0
    curr_consec = 0
    for t in trades:
        if t['pnl_pts'] <= 0:
            curr_consec += 1
            max_consec_loss = max(max_consec_loss, curr_consec)
        else:
            curr_consec = 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t['pnl_pts']
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    # Average hold time
    avg_hold = sum(t['hold_candles'] for t in trades) / len(trades)

    # Number of trading days
    dates = set(t.get('date', '') for t in trades)
    n_days = len(dates) if dates else 1

    pnl_color = '\033[92m' if total_pnl > 0 else '\033[91m'
    reset = '\033[0m'

    print(f"\n{'═' * 55}")
    print(f"  📊 BACKTEST SUMMARY")
    print(f"{'─' * 55}")
    if n_days > 1:
        print(f"  Trading Days:          {n_days}")
    print(f"  Total Trades:          {len(trades)}")
    print(f"  Winners:               {len(winners)} ({win_rate:.1f}%)")
    print(f"  Losers:                {len(losers)}")
    print(f"  {pnl_color}Total P&L:             {total_pnl:+.2f} pts{reset}")
    if n_days > 1:
        print(f"  {pnl_color}Avg P&L/Day:           {total_pnl / n_days:+.2f} pts{reset}")
    print(f"  Avg Win:               {avg_win:+.2f} pts")
    print(f"  Avg Loss:              {avg_loss:+.2f} pts")
    print(f"  Profit Factor:         {profit_factor:.2f}")
    print(f"  Max Consecutive Loss:  {max_consec_loss}")
    print(f"  Max Drawdown:          {max_dd:.2f} pts")
    print(f"  Avg Hold Time:         {avg_hold:.1f} candles (1m)")
    print(f"{'═' * 55}")


def _session_label(entry_time):
    """Return 'Morning', 'Afternoon', or 'Other' for a trade's entry time."""
    t = _time_str_to_minutes(entry_time)
    if t < 11 * 60 + 30:
        return 'Morning'
    elif t >= 13 * 60:
        return 'Afternoon'
    return 'Other'


def _strat_session_stats(trade_list):
    """Return (count, win_rate, total_pnl, avg_pnl, profit_factor) for a list of trades."""
    if not trade_list:
        return 0, 0.0, 0.0, 0.0, 0.0
    wins = sum(1 for t in trade_list if t['pnl_pts'] > 0)
    total = sum(t['pnl_pts'] for t in trade_list)
    avg = total / len(trade_list)
    wr = wins / len(trade_list) * 100
    gw = sum(t['pnl_pts'] for t in trade_list if t['pnl_pts'] > 0)
    gl = abs(sum(t['pnl_pts'] for t in trade_list if t['pnl_pts'] <= 0))
    pf = gw / gl if gl > 0 else float('inf')
    return len(trade_list), wr, total, avg, pf


def print_strategy_breakdown(trades):
    """Break down performance by strategy × session."""
    if not trades:
        return

    # Group by strategy → session
    by_strat_session = defaultdict(lambda: defaultdict(list))
    for t in trades:
        by_strat_session[t['strategy']][_session_label(t['entry_time'])].append(t)

    SESSIONS = ['Morning', 'Afternoon']
    W = 90

    print(f"\n{'═' * W}")
    print(f"  📈 STRATEGY × SESSION BREAKDOWN")
    print(f"{'─' * W}")
    print(f"  {'Strategy':<20} │ {'Session':<10} │ {'Trades':>6} │ {'Win%':>6} │ "
          f"{'P&L':>10} │ {'Avg P&L':>10} │ {'PF':>6}")
    print(f"  {'─' * (W - 2)}")

    for strat in sorted(by_strat_session.keys()):
        session_map = by_strat_session[strat]
        strat_all = [t for sess_trades in session_map.values() for t in sess_trades]

        first = True
        for sess in SESSIONS:
            sess_trades = session_map.get(sess, [])
            if not sess_trades:
                continue
            n, wr, total_pnl, avg_pnl, pf = _strat_session_stats(sess_trades)
            pnl_color = '\033[92m' if total_pnl > 0 else '\033[91m'
            reset = '\033[0m'
            strat_col = strat if first else ''
            first = False
            print(f"  {strat_col:<20} │ {sess:<10} │ {n:>6} │ {wr:>5.1f}% │ "
                  f"{pnl_color}{total_pnl:>+10.2f}{reset} │ {avg_pnl:>+10.2f} │ {pf:>6.2f}")

        # Strategy total row
        n, wr, total_pnl, avg_pnl, pf = _strat_session_stats(strat_all)
        pnl_color = '\033[92m' if total_pnl > 0 else '\033[91m'
        reset = '\033[0m'
        print(f"  {'':20} │ {'TOTAL':<10} │ {n:>6} │ {wr:>5.1f}% │ "
              f"{pnl_color}{total_pnl:>+10.2f}{reset} │ {avg_pnl:>+10.2f} │ {pf:>6.2f}")
        print(f"  {'─' * (W - 2)}")

    print(f"{'═' * W}")



def print_instrument_breakdown(trades):
    """Break down P&L by instrument."""
    if not trades:
        return

    by_inst = defaultdict(list)
    for t in trades:
        by_inst[t['instrument']].append(t)

    print(f"\n{'═' * 50}")
    print(f"  🏛️  INSTRUMENT BREAKDOWN")
    print(f"{'─' * 50}")

    for inst, inst_trades in sorted(by_inst.items()):
        pnl = sum(t['pnl_pts'] for t in inst_trades)
        wins = sum(1 for t in inst_trades if t['pnl_pts'] > 0)
        wr = wins / len(inst_trades) * 100
        pnl_color = '\033[92m' if pnl > 0 else '\033[91m'
        reset = '\033[0m'
        print(f"  {inst:<15} │ {len(inst_trades)} trades │ "
              f"WR {wr:.0f}% │ {pnl_color}P&L {pnl:+.2f} pts{reset}")

    print(f"{'═' * 50}")


def print_daily_breakdown(trades):
    """Break down P&L by date (for multi-day backtests)."""
    dates = set(t.get('date', '') for t in trades)
    if len(dates) <= 1:
        return

    by_date = defaultdict(list)
    for t in trades:
        by_date[t.get('date', 'unknown')].append(t)

    print(f"\n{'═' * 75}")
    print(f"  📅 DAILY BREAKDOWN")
    print(f"{'─' * 75}")
    print(f"  {'Date':<12} │ {'Trades':>6} │ {'Win%':>6} │ {'P&L':>10} │ {'Best':>10} │ {'Worst':>10} │ {'MaxDD':>10}")
    print(f"  {'─' * 70}")

    cum_pnl = 0
    for date_str in sorted(by_date.keys()):
        day_trades = by_date[date_str]
        wins = sum(1 for t in day_trades if t['pnl_pts'] > 0)
        total_pnl = sum(t['pnl_pts'] for t in day_trades)
        wr = wins / len(day_trades) * 100
        best = max(t['pnl_pts'] for t in day_trades)
        worst = min(t['pnl_pts'] for t in day_trades)
        
        # Calculate max drawdown for the day
        cumulative = 0
        peak = 0
        max_dd = 0
        for t in day_trades:
            cumulative += t['pnl_pts']
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        
        cum_pnl += total_pnl

        pnl_color = '\033[92m' if total_pnl > 0 else '\033[91m'
        reset = '\033[0m'
        print(f"  {date_str:<12} │ {len(day_trades):>6} │ {wr:>5.1f}% │ "
              f"{pnl_color}{total_pnl:>+10.2f}{reset} │ {best:>+10.2f} │ {worst:>+10.2f} │ {max_dd:>10.2f}")

    print(f"  {'─' * 70}")
    pnl_color = '\033[92m' if cum_pnl > 0 else '\033[91m'
    reset = '\033[0m'
    print(f"  {'TOTAL':<12} │ {len(trades):>6} │        │ {pnl_color}{cum_pnl:>+10.2f}{reset} │")
    print(f"{'═' * 75}")


def _time_str_to_minutes(time_str):
    """Convert 'HH:MM' string to minutes from midnight."""
    try:
        parts = time_str.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 0


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description='Multi-Timeframe Scalping Backtester',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtest.py                             # backtest today
  python backtest.py --date 2026-03-10           # backtest a specific date
  python backtest.py --date 2026-03-10 --date 2026-03-11  # multi-day
  python backtest.py --from 2026-03-07 --to 2026-03-11    # date range
  python backtest.py --date 2026-03-10 --no-bias          # without 5m bias filter
  python backtest.py --from 2026-03-05 --to 2026-03-13 --instrument nifty      # only NIFTY 50
  python backtest.py --from 2026-03-05 --to 2026-03-13 --instrument banknifty  # only BANK NIFTY
        """
    )
    parser.add_argument('--date', action='append', metavar='YYYY-MM-DD',
                        help='Date(s) to backtest. Can specify multiple times.')
    parser.add_argument('--from', dest='from_date', metavar='YYYY-MM-DD',
                        help='Start date for range backtest (inclusive)')
    parser.add_argument('--to', dest='to_date', metavar='YYYY-MM-DD',
                        help='End date for range backtest (inclusive)')
    parser.add_argument('--no-bias', dest='no_bias', action='store_true',
                        help='Disable 5m bias filter — accept all signals (like current live system)')
    parser.add_argument('--instrument', choices=['nifty', 'banknifty'], 
                        help='Run backtest on specific instrument only (nifty or banknifty)')
    return parser.parse_args()


def get_dates_to_backtest(args):
    """Resolve CLI args into a list of date strings to backtest."""
    today = datetime.now(IST).strftime('%Y-%m-%d')

    # Date range mode: --from X --to Y
    if args.from_date and args.to_date:
        start = datetime.strptime(args.from_date, '%Y-%m-%d')
        end = datetime.strptime(args.to_date, '%Y-%m-%d')
        dates = []
        current = start
        while current <= end:
            # Skip weekends (5=Saturday, 6=Sunday)
            if current.weekday() < 5:
                dates.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
        return dates

    # Explicit dates mode: --date X --date Y
    if args.date:
        return args.date

    # Default: today
    return [today]


def main():
    args = parse_args()
    dates = get_dates_to_backtest(args)

    no_bias = args.no_bias

    # Filter instruments based on --instrument arg
    instruments_to_test = INSTRUMENTS
    if args.instrument:
        if args.instrument == 'nifty':
            instruments_to_test = [inst for inst in INSTRUMENTS if inst['name'] == 'NIFTY 50']
        elif args.instrument == 'banknifty':
            instruments_to_test = [inst for inst in INSTRUMENTS if inst['name'] == 'BANK NIFTY']

    print("\n" + "═" * 60)
    print("  🔬 MULTI-TIMEFRAME SCALPING BACKTESTER")
    if no_bias:
        print("  Mode: NO BIAS FILTER (all signals accepted)")
    else:
        print("  Architecture: 5m Bias (4 indicators) → 1m Trigger")
        print("  SMC_OB & EMA33_OB: Independent (use own logic)")
    print("═" * 60)


    if len(dates) == 1:
        print(f"\n  Date: {dates[0]}")
    else:
        print(f"\n  Dates: {dates[0]} → {dates[-1]} ({len(dates)} trading days)")

    if args.instrument:
        print(f"  Instrument: {args.instrument.upper()}")
    else:
        print(f"  Instruments: ALL ({len(instruments_to_test)})")

    print(f"  Bias Filter: {'OFF ❌' if no_bias else 'ON ✅'}")
    print(f"  Config: SL={SCALP_CONFIG['sl_atr_mult']}×ATR  TP={SCALP_CONFIG['tp_atr_mult']}×ATR  "
          f"MaxHold={SCALP_CONFIG['max_hold_candles']}m  "
          f"Trades=AM:{SCALP_CONFIG['morning_trade_limit']}/PM:{SCALP_CONFIG['afternoon_trade_limit']}  "
          f"Cooldown={SCALP_CONFIG['bias_cooldown_candles']}m")
    print(f"  Gap Handling: >{SCALP_CONFIG['gap_threshold_pct']}% gap → Price action bias for "
          f"{SCALP_CONFIG['gap_price_action_duration']} mins, then 5m consensus")
    print(f"  Sessions: 09:20-11:30 + 13:00-14:30 │ Force exit: 15:15")
    print(f"  Session routing: VWAP_ST=morning-only  SMC_OB=afternoon-only")
    print(f"  Daily loss cap: {SCALP_CONFIG['daily_loss_cap']} pts")

    all_trades = []

    for date_str in dates:
        if len(dates) > 1:
            print(f"\n\n{'▓' * 60}")
            print(f"  📆 BACKTESTING: {date_str}")
            print(f"{'▓' * 60}")

        for inst in instruments_to_test:
            sec_id = inst['sec_id']
            name = inst['name']
            print(f"\n{'─' * 60}")
            print(f"  📌 {name} (sec_id={sec_id}) — {date_str}")
            print(f"{'─' * 60}")

            data_1m, data_5m = fetch_candles(sec_id, name, date_str)
            if not data_1m or not data_5m:
                print(f"  ✗ Skipping {name} — could not fetch candle data")
                continue

            trades = run_backtest(sec_id, name, data_1m, data_5m, no_bias=no_bias)
            all_trades.extend(trades)

    # ── Print all analytics ──
    date_label = dates[0] if len(dates) == 1 else f"{dates[0]} → {dates[-1]}"
    print(f"\n\n{'█' * 60}")
    print(f"  🏁 BACKTEST RESULTS — {date_label}")
    print(f"{'█' * 60}")

    print_trade_log(all_trades)
    print_summary(all_trades)
    print_strategy_breakdown(all_trades)
    print_instrument_breakdown(all_trades)
    print_daily_breakdown(all_trades)

    print(f"\n  Done! {len(all_trades)} total trades simulated across {len(dates)} day(s).\n")


if __name__ == '__main__':
    main()
