"""Multi-strategy walk-forward backtester — speed-optimized.

Optimizations:
  1. Data cached as .npz (skip JSON parsing on reload)
  2. Bias/ATR/trend computed ONCE on full arrays (not per-bar windowed)
  3. Numpy views for strategy slices (O(1), no copy)
  4. Fast IST minute math (no datetime objects in hot loop)
  5. Binary search for TF index lookups
  6. Reusable MockChainSnapshot (avoid object creation)
"""

from __future__ import annotations

import json
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_S = 19800

CONCURRENT_BY_DEFAULT = {"smc_order_block", "ttm_squeeze", "ema33_ob"}
SL_CAPS = {"NIFTY_50": 20, "BANK_NIFTY": 40, "SENSEX": 60}

DEFAULT_EXIT = {
    "sl_atr_mult": 0.5,
    "tp_atr_mult": 1.5,
    "max_hold_bars": 20,
    "slippage_pts": 0.5,
}

_MAX_5M_LOOKBACK = 200


# ── Data loading with .npz cache ─────────────────────────────────────────────

def _load_1m_data(data_dir: Path, instrument: str, start_date, end_date) -> dict:
    inst_dir = data_dir / instrument
    if not inst_dir.exists():
        return {}

    sd = _date.fromisoformat(str(start_date)) if isinstance(start_date, str) else start_date
    ed = _date.fromisoformat(str(end_date)) if isinstance(end_date, str) else end_date
    warmup_start = sd - timedelta(days=25)

    cache_key = f"{instrument}_{warmup_start}_{ed}"
    cache_path = data_dir / f".cache_{cache_key}.npz"

    if cache_path.exists():
        try:
            d = np.load(str(cache_path))
            return {k: d[k] for k in ("open", "high", "low", "close", "volume", "timestamp")}
        except Exception:
            pass

    all_o, all_h, all_l, all_c, all_v, all_ts = [], [], [], [], [], []

    for f in sorted(inst_dir.glob("*_1m.json")):
        try:
            file_date = _date.fromisoformat(f.stem.replace("_1m", ""))
            if file_date < warmup_start or file_date > ed:
                continue
        except ValueError:
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue

        ts_arr = data.get("timestamp", [])
        vol_arr = data.get("volume", [])
        n = len(ts_arr)
        all_o.extend(data["open"][:n])
        all_h.extend(data["high"][:n])
        all_l.extend(data["low"][:n])
        all_c.extend(data["close"][:n])
        all_v.extend(vol_arr[:n] if vol_arr else [0.0] * n)
        all_ts.extend(ts_arr)

    if not all_c:
        return {}

    idx = np.argsort(all_ts)
    result = {
        "open": np.array(all_o, dtype=np.float64)[idx],
        "high": np.array(all_h, dtype=np.float64)[idx],
        "low": np.array(all_l, dtype=np.float64)[idx],
        "close": np.array(all_c, dtype=np.float64)[idx],
        "volume": np.array(all_v, dtype=np.float64)[idx],
        "timestamp": np.array(all_ts, dtype=np.float64)[idx],
    }

    try:
        np.savez_compressed(str(cache_path), **result)
    except Exception:
        pass

    return result


def _aggregate_np(timestamps, opens, highs, lows, closes, volumes, period_secs):
    """Aggregate 1m numpy arrays → higher TF. Returns dict of numpy arrays."""
    if len(timestamps) == 0:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty,
                "volume": empty, "timestamp": empty}

    periods = (timestamps // period_secs * period_secs).astype(np.float64)
    breaks = np.where(np.diff(periods) != 0)[0] + 1
    splits_start = np.concatenate([[0], breaks])
    splits_end = np.concatenate([breaks, [len(timestamps)]])

    # Drop the last chunk (partial bar — no future leak)
    n_bars = len(splits_start) - 1
    if n_bars < 1:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty,
                "volume": empty, "timestamp": empty}

    r_ts = np.empty(n_bars, dtype=np.float64)
    r_o = np.empty(n_bars, dtype=np.float64)
    r_h = np.empty(n_bars, dtype=np.float64)
    r_l = np.empty(n_bars, dtype=np.float64)
    r_c = np.empty(n_bars, dtype=np.float64)
    r_v = np.empty(n_bars, dtype=np.float64)

    for k in range(n_bars):
        s, e = splits_start[k], splits_end[k]
        r_ts[k] = periods[s]
        r_o[k] = opens[s]
        r_h[k] = highs[s:e].max()
        r_l[k] = lows[s:e].min()
        r_c[k] = closes[e - 1]
        r_v[k] = volumes[s:e].sum()

    return {"open": r_o, "high": r_h, "low": r_l, "close": r_c,
            "volume": r_v, "timestamp": r_ts}


# ── Full-array indicator functions (compute ONCE) ─────────────────────────────

def _ema_full(closes, period):
    """EMA on full array. Returns array of length len(closes) - period + 1."""
    n = len(closes)
    if n < period or period < 1:
        return np.array([])
    k = 2.0 / (period + 1)
    result = np.empty(n - period + 1)
    result[0] = np.mean(closes[:period])
    for i in range(1, len(result)):
        result[i] = closes[period + i - 1] * k + result[i - 1] * (1.0 - k)
    return result


def _atr_full(highs, lows, closes, period):
    """Wilder ATR on full array. Returns array of length len(closes) - period."""
    n = len(closes)
    if n < period + 1:
        return np.array([])

    tr = np.empty(n - 1)
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i - 1] = max(hl, hc, lc)

    if len(tr) < period:
        return np.array([])

    result = np.empty(len(tr) - period + 1)
    result[0] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(1, len(result)):
        result[i] = result[i - 1] * (1 - alpha) + tr[period + i - 1] * alpha
    return result


def _rsi_full(closes, period=14):
    """Wilder RSI on full array. Returns array of length len(closes) - period."""
    n = len(closes)
    if n < period + 1:
        return np.array([])

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    result = np.empty(len(deltas) - period + 1)
    if avg_loss == 0:
        result[0] = 100.0
    else:
        result[0] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(1, len(result)):
        avg_gain = (avg_gain * (period - 1) + gains[period + i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[period + i - 1]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    return result


def _supertrend_full(highs, lows, closes, period=10, multiplier=3.0):
    """Supertrend on full array. Returns direction array aligned to ATR output."""
    atr_arr = _atr_full(highs, lows, closes, period)
    if len(atr_arr) < 3:
        return None

    offset = period  # ATR starts at index `period` of closes
    n = len(atr_arr)
    upper = np.empty(n)
    lower = np.empty(n)
    st = np.empty(n)
    direction = np.ones(n, dtype=np.int8)

    for i in range(n):
        ci = offset + i
        hl2 = (highs[ci] + lows[ci]) / 2.0
        upper[i] = hl2 + multiplier * atr_arr[i]
        lower[i] = hl2 - multiplier * atr_arr[i]
        if i == 0:
            st[i] = upper[i]
            direction[i] = -1 if closes[ci] < st[i] else 1
            continue
        if closes[ci - 1] > lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1])
        if closes[ci - 1] < upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1])
        prev = st[i - 1]
        if prev == upper[i - 1]:
            if closes[ci] <= upper[i]:
                st[i] = upper[i]
                direction[i] = -1
            else:
                st[i] = lower[i]
                direction[i] = 1
        else:
            if closes[ci] >= lower[i]:
                st[i] = lower[i]
                direction[i] = 1
            else:
                st[i] = upper[i]
                direction[i] = -1

    return direction


def _ttm_momentum_full(closes, period=20):
    """TTM Squeeze momentum on full array. Returns array aligned to bar index."""
    n = len(closes)
    if n < period:
        return np.array([])
    result = np.empty(n - period + 1)
    for i in range(len(result)):
        window = closes[i:i + period]
        midline = (window.max() + window.min()) / 2
        result[i] = closes[i + period - 1] - midline
    return result


# ── Vectorized bias pre-computation ──────────────────────────────────────────

def _precompute_all(data_5m, data_15m, bias_cfg):
    """Pre-compute bias, ATR(14), 15m trend for ALL bars using full-array indicators.

    This computes each indicator ONCE on the full array instead of 19k windowed calls.
    Results are functionally identical (same buy/sell decisions).
    """
    closes_5m = data_5m["close"]
    highs_5m = data_5m["high"]
    lows_5m = data_5m["low"]
    n5 = len(closes_5m)

    cfg = bias_cfg or {}
    min_agreement = cfg.get("min_agreement", 2)

    # ── Compute indicators ONCE ──────────────────────────────────────
    ema_short_arr = ema_long_arr = None
    if cfg.get("use_ema_bias", True):
        sp = cfg.get("ema_short", 2)
        lp = cfg.get("ema_long", 11)
        ema_short_arr = _ema_full(closes_5m, sp)   # len = n5 - sp + 1, starts at bar sp-1
        ema_long_arr = _ema_full(closes_5m, lp)     # len = n5 - lp + 1, starts at bar lp-1
        ema_short_offset = sp - 1
        ema_long_offset = lp - 1

    st_dir_arr = None
    st_period = cfg.get("st_period", 10)
    if cfg.get("use_supertrend", True):
        st_dir_arr = _supertrend_full(highs_5m, lows_5m, closes_5m, st_period, cfg.get("st_multiplier", 3.0))
        # direction[i] corresponds to closes[st_period + i]
        st_offset = st_period

    ttm_arr = None
    if cfg.get("use_ttm_squeeze", True):
        ttm_arr = _ttm_momentum_full(closes_5m, 20)
        # ttm[i] corresponds to closes[19 + i]
        ttm_offset = 19

    ema33_arr = rsi14_arr = None
    if cfg.get("use_ema33_zone", True):
        ema33_arr = _ema_full(closes_5m, 33)    # starts at bar 32
        rsi14_arr = _rsi_full(closes_5m, 14)    # starts at bar 14
        ema33_offset = 32
        rsi14_offset = 14

    # ── Build bias array ─────────────────────────────────────────────
    bias_cache = [None] * n5

    for b in range(n5):
        votes_buy = 0
        votes_sell = 0

        # EMA bias
        if ema_short_arr is not None and ema_long_arr is not None:
            si = b - ema_short_offset
            li = b - ema_long_offset
            if si >= 0 and si < len(ema_short_arr) and li >= 0 and li < len(ema_long_arr):
                if ema_short_arr[si] > ema_long_arr[li]:
                    votes_buy += 1
                elif ema_short_arr[si] < ema_long_arr[li]:
                    votes_sell += 1

        # Supertrend
        if st_dir_arr is not None:
            si = b - st_offset
            if 0 <= si < len(st_dir_arr):
                if st_dir_arr[si] == 1:
                    votes_buy += 1
                elif st_dir_arr[si] == -1:
                    votes_sell += 1

        # TTM momentum
        if ttm_arr is not None:
            ti = b - ttm_offset
            if 0 <= ti < len(ttm_arr):
                if ttm_arr[ti] > 0:
                    votes_buy += 1
                elif ttm_arr[ti] < 0:
                    votes_sell += 1

        # EMA33 zone
        if ema33_arr is not None and rsi14_arr is not None:
            ei = b - ema33_offset
            ri = b - rsi14_offset
            if 0 <= ei < len(ema33_arr) and 0 <= ri < len(rsi14_arr):
                if closes_5m[b] > ema33_arr[ei] and rsi14_arr[ri] > 60:
                    votes_buy += 1
                elif closes_5m[b] < ema33_arr[ei] and rsi14_arr[ri] < 40:
                    votes_sell += 1

        if votes_buy >= min_agreement:
            bias_cache[b] = "BUY"
        elif votes_sell >= min_agreement:
            bias_cache[b] = "SELL"

    # ── ATR(14) for all bars ─────────────────────────────────────────
    atr_full = _atr_full(highs_5m, lows_5m, closes_5m, 14)
    atr_cache = [None] * n5
    atr_offset = 14  # atr_full[i] corresponds to closes[14 + i]
    for i in range(len(atr_full)):
        atr_cache[atr_offset + i] = atr_full[i]

    # ── 15m trend ────────────────────────────────────────────────────
    closes_15m = data_15m.get("close", np.array([]))
    n15 = len(closes_15m)
    trend_cache = [None] * n15
    if n15 >= 16:
        ema9 = _ema_full(closes_15m, 9)    # starts at bar 8
        ema15 = _ema_full(closes_15m, 15)  # starts at bar 14
        for b in range(n15):
            si = b - 8
            li = b - 14
            if si >= 0 and si < len(ema9) and li >= 0 and li < len(ema15):
                if ema9[si] > ema15[li]:
                    trend_cache[b] = "BUY"
                elif ema9[si] < ema15[li]:
                    trend_cache[b] = "SELL"

    return bias_cache, atr_cache, trend_cache


# ── Strategy caller ───────────────────────────────────────────────────────────

def _ensure_sys_path():
    for root in [Path("/app"), Path(__file__).parent.parent]:
        if (root / "services" / "user_worker_pool" / "strategies").exists():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            break


def _create_strategy(name: str):
    _ensure_sys_path()
    from services.user_worker_pool.strategies import STRATEGY_REGISTRY
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown strategy: {name!r}. Available: {sorted(STRATEGY_REGISTRY)}")
    return cls()


def _call_strategy_fast(strategy, chain, regime, params=None) -> str | None:
    """Call strategy.evaluate() with a pre-built chain. Returns 'BUY'/'SELL' or None."""
    config = {"instruments": [chain.underlying]}
    if params:
        config.update(params)
    try:
        signal = strategy.evaluate(chain, regime, [], config)
    except Exception:
        return None
    if signal is None:
        return None
    return "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"


# ── Pre-compute helpers ───────────────────────────────────────────────────────

def _build_5m_close_set(ts_1m, ts_5m) -> set:
    close_set = set()
    if len(ts_5m) == 0:
        return close_set
    deadlines = ts_5m + 300
    j = 0
    for deadline in deadlines:
        last = -1
        while j < len(ts_1m) and ts_1m[j] < deadline:
            last = j
            j += 1
        if last >= 0:
            close_set.add(last)
    return close_set


# ── Main engine ───────────────────────────────────────────────────────────────

def run(config: dict) -> dict:
    instrument = config.get("instrument", "NIFTY_50")
    start_date = config.get("start_date", "2024-01-01")
    end_date = config.get("end_date", "2025-12-31")
    data_dir = Path(config.get("data_dir", "/data"))
    bias_cfg = config.get("bias_config", {})
    slots = config.get("strategies", [])
    exit_cfg = {**DEFAULT_EXIT, **config.get("exit_config", {})}

    if not slots:
        return _empty_result()

    # ── Load ──────────────────────────────────────────────────────────
    data_1m = _load_1m_data(data_dir, instrument, start_date, end_date)
    if not data_1m or len(data_1m.get("close", [])) == 0:
        return _empty_result()

    # Ensure numpy arrays
    for k in ("open", "high", "low", "close", "volume", "timestamp"):
        if not isinstance(data_1m[k], np.ndarray):
            data_1m[k] = np.array(data_1m[k], dtype=np.float64)

    timestamps = data_1m["timestamp"]
    closes = data_1m["close"]
    highs = data_1m["high"]
    lows_arr = data_1m["low"]

    sd = _date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    actual_start_ts = datetime(sd.year, sd.month, sd.day, 9, 0, tzinfo=IST).timestamp()

    # ── Per-strategy bias pre-computation ────────────────────────────
    from services.user_worker_pool.bias.evaluator import BiasEvaluator, aggregate_candles, atr_full

    # Build 5m candles from 1m (needed for strategy signals + ATR)
    data_5m = aggregate_candles(data_1m, 300)
    ts_5m = data_5m.get("timestamp", np.array([]))
    n_5m = len(data_5m.get("close", []))

    # Build 15m and 1H candles (required by brahmaastra, ema5_mean_reversion, parent_child)
    data_15m = aggregate_candles(data_1m, 900)
    data_1h  = aggregate_candles(data_1m, 3600)

    # Compute ATR(14) on 5m — shared across all strategies
    atr_arr = atr_full(data_5m["high"], data_5m["low"], data_5m["close"], 14) if n_5m > 15 else np.array([])
    atr_cache: list[float | None] = [None] * n_5m
    for ai in range(len(atr_arr)):
        if 14 + ai < n_5m:
            atr_cache[14 + ai] = float(atr_arr[ai])

    # ── Pre-compute strategy signals FIRST ───────────────────────────
    # Must happen before bias precomputation so strategy_instance bias filters
    # can reference precomputed signal arrays from other slots.
    from .fast_strategies import precompute_strategy_signals, build_prev_day_arrays

    # Pre-build prev-day arrays for Brahmaastra once (data-derived, not per-combo)
    _brahmaastra_prev_day: dict = {}
    _n15 = len(data_15m.get("close", []))
    _n1h = len(data_1h.get("close", []))
    if any(slot["name"] == "brahmaastra" for slot in slots) and _n15 > 0:
        pdc_arr, pdh_arr, pdl_arr = build_prev_day_arrays(
            data_1m["close"], data_1m["high"], data_1m["low"],
            data_1m["timestamp"], data_15m["timestamp"],
        )
        _brahmaastra_prev_day = {"pdc_arr": pdc_arr, "pdh_arr": pdh_arr, "pdl_arr": pdl_arr}

    fast_signals: dict[str, np.ndarray | None] = {}
    for slot in slots:
        name = slot["name"]
        if name not in fast_signals:
            opens_5m = data_5m.get("open", data_5m["close"])
            slot_params = dict(slot.get("params") or {})
            if name == "brahmaastra":
                slot_params.update(_brahmaastra_prev_day)
            fast_signals[name] = precompute_strategy_signals(
                name, data_5m["close"], data_5m["high"], data_5m["low"],
                opens_5m, slot_params,
                closes_15m=data_15m.get("close")     if _n15 > 0 else None,
                highs_15m=data_15m.get("high")       if _n15 > 0 else None,
                lows_15m=data_15m.get("low")         if _n15 > 0 else None,
                opens_15m=data_15m.get("open")       if _n15 > 0 else None,
                timestamps_15m=data_15m.get("timestamp") if _n15 > 0 else None,
                closes_1h=data_1h.get("close")       if _n1h > 0 else None,
            )

    # ── Per-strategy bias caches (keyed by strategy name) ────────────
    # Each strategy can have its own bias_config, or fall back to global.
    # fast_signals is passed so strategy_instance filters can reference them.
    strategy_bias_caches: dict[str, list] = {}
    _bias_config_cache: dict[str, tuple] = {}  # deduplicate identical configs

    for slot in slots:
        s_name = slot["name"]
        if s_name in strategy_bias_caches:
            continue

        # Priority: per-strategy bias_config > global bias_config
        s_bias_cfg = slot.get("bias_config") or bias_cfg
        s_mode = s_bias_cfg.get("mode", slot.get("mode", "independent"))

        if s_mode != "bias_filtered" or not s_bias_cfg.get("bias_filters"):
            strategy_bias_caches[s_name] = [None] * n_5m
            continue

        # Deduplicate: if two strategies share identical bias config, reuse
        config_key = str(sorted(str(s_bias_cfg).lower()))
        if config_key in _bias_config_cache:
            strategy_bias_caches[s_name] = _bias_config_cache[config_key]
            continue

        evaluator = BiasEvaluator(s_bias_cfg)
        bias_list, _ = evaluator.precompute_bias_array(data_1m, precomputed_signals=fast_signals)
        strategy_bias_caches[s_name] = bias_list
        _bias_config_cache[config_key] = bias_list

    # ── 5m close events ───────────────────────────────────────────────
    five_m_close_set = _build_5m_close_set(timestamps, ts_5m)

    _bar_date_ref = [_date.today()]

    # Pre-build 5m numpy arrays (views will be taken from these)
    np_5m_open = data_5m["open"]
    np_5m_close = data_5m["close"]
    np_5m_high = data_5m["high"]
    np_5m_low = data_5m["low"]

    # ── Walk-forward state ────────────────────────────────────────────
    n = len(closes)
    all_trades: list[dict] = []
    open_trade: dict | None = None
    open_concurrent: list[dict] = []

    daily_fires: dict[str, int] = defaultdict(int)

    cumulative_pnl = 0.0
    peak_pnl = 0.0
    equity_snapshots: list[dict] = []
    prev_day_num = -1

    sl_cap = SL_CAPS.get(instrument, 30)
    LOT_SIZES = {"NIFTY_50": 75, "BANK_NIFTY": 30, "SENSEX": 10}
    lot_size = LOT_SIZES.get(instrument, 50)

    EQUITY_EVERY = 15
    # Global fallback session bounds (overridden per-strategy in the inner loop)
    morning_end     = 690   # 11:30 IST
    afternoon_start = 780   # 13:00 IST

    from .fast_strategies import get_strategy_session
    # Pre-build per-strategy session config once
    _slot_session: dict[str, dict] = {
        slot["name"]: get_strategy_session(slot["name"]) for slot in slots
    }

    # ── Build sorted list of 5m close 1m-indices ─────────────────────
    five_m_close_indices = sorted(five_m_close_set)
    # Filter to those >= start
    start_idx = int(np.searchsorted(timestamps, actual_start_ts))
    five_m_close_indices = [idx for idx in five_m_close_indices if idx >= start_idx]

    # Pre-compute IST minutes for all 1m bars (vectorized)
    ist_secs = (timestamps.astype(np.int64) + IST_OFFSET_S)
    all_t_min = ((ist_secs % 86400) // 60).astype(np.int32)
    all_day_num = (ist_secs // 86400).astype(np.int32)

    # ── Vectorized exit finder ────────────────────────────────────────
    def _find_exit_vectorized(ot: dict) -> tuple:
        """Find exit bar for a trade using numpy. Returns (reason, exit_price, exit_bar_idx)."""
        entry_bar = ot["entry_bar"]
        max_hold = ot.get("max_hold", exit_cfg["max_hold_bars"])
        d = ot["direction"]
        sl_val = ot["sl"]
        tp_val = ot["tp"]
        # Use strategy-specific kill-switch time (e.g. 10:30 for brahmaastra, 15:15 for others)
        _force_min = ot.get("force_exit_min", 915)

        # Scan window from entry+1 to entry+max_hold (or end of day)
        end_bar = min(entry_bar + max_hold + 1, n)
        if entry_bar + 1 >= n:
            return "end_of_backtest", float(closes[-1]), n - 1

        h_slice = highs[entry_bar + 1:end_bar]
        l_slice = lows_arr[entry_bar + 1:end_bar]
        c_slice = closes[entry_bar + 1:end_bar]
        tmin_slice = all_t_min[entry_bar + 1:end_bar]

        if len(h_slice) == 0:
            return "end_of_backtest", float(closes[-1]), n - 1

        # SL/TP hit detection
        if d == "BUY":
            sl_hits = np.where(l_slice <= sl_val)[0]
            tp_hits = np.where(h_slice >= tp_val)[0]
        else:
            sl_hits = np.where(h_slice >= sl_val)[0]
            tp_hits = np.where(l_slice <= tp_val)[0]

        # Force exit at strategy-specific kill-switch time
        force_hits = np.where(tmin_slice >= _force_min)[0]

        # Find earliest event
        sl_bar = int(sl_hits[0]) if len(sl_hits) > 0 else len(h_slice) + 1
        tp_bar = int(tp_hits[0]) if len(tp_hits) > 0 else len(h_slice) + 1
        force_bar = int(force_hits[0]) if len(force_hits) > 0 else len(h_slice) + 1
        time_bar = int(max_hold)

        # Which comes first?
        min_bar = min(sl_bar, tp_bar, force_bar, time_bar)

        if min_bar >= len(h_slice):
            abs_bar = int(min(entry_bar + max_hold, n - 1))
            return "time_stop", float(closes[abs_bar]), abs_bar

        abs_bar = int(entry_bar + 1 + min_bar)

        if tp_bar <= sl_bar and tp_bar == min_bar:
            return "target", float(tp_val), abs_bar
        if sl_bar == min_bar:
            return "stop_loss", float(sl_val), abs_bar
        if force_bar == min_bar:
            return "square_off", float(closes[abs_bar]), abs_bar
        return "time_stop", float(closes[abs_bar]), abs_bar

    # ── Main loop: iterate only over 5m close indices ─────────────────
    for idx_pos, i in enumerate(five_m_close_indices):
        ts = timestamps[i]
        price = closes[i]
        t_min = int(all_t_min[i])

        # Day boundary
        day_num = int(all_day_num[i])
        if day_num != prev_day_num:
            prev_day_num = day_num
            daily_fires.clear()

        # ── Check exits (cached — computed once at trade open) ─────────
        if open_trade is not None:
            if open_trade["_exit_bar"] <= i:
                r, ep, eb = open_trade["_exit_reason"], open_trade["_exit_price"], open_trade["_exit_bar"]
                exit_ts = float(timestamps[eb])
                cumulative_pnl += _close_trade(open_trade, r, ep, eb, exit_ts, exit_cfg, lot_size, all_trades)
                if cumulative_pnl > peak_pnl:
                    peak_pnl = cumulative_pnl
                open_trade = None

        if open_concurrent:
            still_open = []
            for ot in open_concurrent:
                if ot["_exit_bar"] <= i:
                    r, ep, eb = ot["_exit_reason"], ot["_exit_price"], ot["_exit_bar"]
                    exit_ts = float(timestamps[eb])
                    cumulative_pnl += _close_trade(ot, r, ep, eb, exit_ts, exit_cfg, lot_size, all_trades)
                    if cumulative_pnl > peak_pnl:
                        peak_pnl = cumulative_pnl
                else:
                    still_open.append(ot)
            open_concurrent = still_open

        # ── 5m close: lookup pre-computed values ──────────────────────
        n5 = int(np.searchsorted(ts_5m, ts - 300, side="right"))
        if n5 < 15:
            continue

        atr = atr_cache[n5 - 1] if n5 - 1 < len(atr_cache) else None

        # Broad gate: any slot's window could be open — defer fine filtering to per-slot
        if not atr or atr <= 0:
            if idx_pos % 5 == 0:
                dd = peak_pnl - cumulative_pnl
                equity_snapshots.append({
                    "timestamp": float(ts),
                    "equity": round(cumulative_pnl, 2),
                    "drawdown_pct": round((dd / max(abs(peak_pnl), 1) * 100) if peak_pnl != 0 else 0, 2),
                })
            continue

        # ── Strategy evaluation (per-strategy bias) ───────────────────

        for slot in slots:
            s_name = slot["name"]
            s_session = slot.get("session", "all")
            s_concurrent = slot.get("concurrent", s_name in CONCURRENT_BY_DEFAULT)
            # Max fires: slot override first, else per-strategy default from shared config
            _sc = _slot_session[s_name]
            s_max_fires = slot.get("max_fires_per_day", _sc["max_fires"])
            s_time_stop = slot.get("time_stop_bars", exit_cfg["max_hold_bars"])
            s_force_exit = _sc["force_exit_min"]
            s_unified    = _sc["unified_window"]
            s_entry_start  = _sc["entry_start"]
            s_entry_cutoff = _sc["entry_cutoff"]

            # Per-strategy session window check
            if s_unified:
                if not (s_entry_start <= t_min <= s_entry_cutoff):
                    continue
            else:
                s_morning   = s_entry_start <= t_min <= morning_end
                s_afternoon = afternoon_start <= t_min <= s_entry_cutoff
                if s_session == "morning" and not s_morning:
                    continue
                if s_session == "afternoon" and not s_afternoon:
                    continue
                if not (s_morning or s_afternoon):
                    continue

            if daily_fires.get(s_name, 0) >= s_max_fires:
                continue

            # Per-strategy bias check
            s_bias_cache = strategy_bias_caches.get(s_name, [])
            s_bias = s_bias_cache[n5 - 1] if n5 - 1 < len(s_bias_cache) else None
            # Bias active when cache was built with bias filters (non-None entries exist).
            # Use the same rule as cache construction: bias_cfg.mode or slot.mode.
            s_bias_cfg = slot.get("bias_config") or bias_cfg
            s_bias_mode = s_bias_cfg.get("mode", slot.get("mode", "independent"))
            if s_bias_mode == "bias_filtered":
                if not s_bias:
                    continue
            if s_concurrent:
                if any(ot["strategy"] == s_name for ot in open_concurrent):
                    continue
            elif open_trade is not None:
                continue

            # Direct signal from pre-computed arrays (no mock objects, pure OHLCV)
            fast_sig = fast_signals.get(s_name)
            if fast_sig is None:
                continue  # no fast path = strategy not supported in backtest
            sig_val = int(fast_sig[n5 - 1]) if n5 - 1 < len(fast_sig) else 0
            if sig_val == 1:
                sig_dir = "BUY"
            elif sig_val == -1:
                sig_dir = "SELL"
            else:
                continue

            # Signal must align with this strategy's bias direction
            if s_bias_mode == "bias_filtered" and s_bias and sig_dir != s_bias:
                continue

            slip = exit_cfg["slippage_pts"]
            entry = float(price) + (slip if sig_dir == "BUY" else -slip)
            s_params = slot.get("params", {})
            strategy_sl_cap = s_params.get("max_sl_points", sl_cap)
            # Per-slot exit multipliers take priority over global exit_cfg
            _sl_mult = slot.get("sl_atr_mult", exit_cfg["sl_atr_mult"])
            _tp_mult = slot.get("tp_atr_mult", exit_cfg["tp_atr_mult"])
            sl_dist = min(_sl_mult * atr, strategy_sl_cap)
            tp_dist = _tp_mult * atr

            if sig_dir == "BUY":
                sl, tp = entry - sl_dist, entry + tp_dist
            else:
                sl, tp = entry + sl_dist, entry - tp_dist

            new_trade = {
                "direction": sig_dir, "entry_price": entry,
                "sl": sl, "tp": tp, "entry_bar": i, "entry_ts": float(ts),
                "strategy": s_name, "atr": atr, "bias": s_bias,
                "max_hold": s_time_stop, "force_exit_min": s_force_exit,
            }

            # Compute exit immediately (vectorized, O(max_hold))
            reason, ep, eb = _find_exit_vectorized(new_trade)
            new_trade["_exit_reason"] = reason
            new_trade["_exit_price"] = ep
            new_trade["_exit_bar"] = eb

            if s_concurrent:
                open_concurrent.append(new_trade)
            else:
                open_trade = new_trade

            daily_fires[s_name] = daily_fires.get(s_name, 0) + 1

        dd = peak_pnl - cumulative_pnl
        equity_snapshots.append({
            "timestamp": float(ts),
            "equity": round(cumulative_pnl, 2),
            "drawdown_pct": round((dd / max(abs(peak_pnl), 1) * 100) if peak_pnl != 0 else 0, 2),
        })

    # ── Force-close remaining ─────────────────────────────────────────
    for ot in ([open_trade] if open_trade else []) + open_concurrent:
        lp = float(closes[-1])
        lt = float(timestamps[-1]) if len(timestamps) > 0 else 0
        pnl_pts = (lp - ot["entry_price"]) if ot["direction"] == "BUY" else (ot["entry_price"] - lp)
        pnl_pts -= exit_cfg["slippage_pts"]
        cumulative_pnl += pnl_pts
        all_trades.append({
            "strategy_name": ot["strategy"],
            "direction": 1 if ot["direction"] == "BUY" else -1,
            "entry_price": round(ot["entry_price"], 2),
            "exit_price": round(lp, 2),
            "stop_loss": round(ot["sl"], 2), "target": round(ot["tp"], 2),
            "hold_candles": n - 1 - ot["entry_bar"],
            "exit_bar_ts": lt, "quantity": 1, "lot_size": lot_size,
            "pnl": round(pnl_pts * lot_size, 2),
            "slippage": exit_cfg["slippage_pts"], "brokerage": 20.0,
            "exit_reason": "end_of_backtest", "tag": ot["direction"],
        })

    # ── Build result ──────────────────────────────────────────────────
    strategy_names = list({s["name"] for s in slots})
    per_strat_trades = {name: [t for t in all_trades if t["strategy_name"] == name] for name in strategy_names}

    # Per-strategy bias stats
    per_strategy_bias: dict[str, dict] = {}
    for slot in slots:
        s_name = slot["name"]
        bias_list = strategy_bias_caches.get(s_name, [])
        s_bias_cfg = slot.get("bias_config") or bias_cfg
        is_filtered = s_bias_cfg.get("mode") == "bias_filtered" or slot.get("mode") == "bias_filtered"

        if bias_list and is_filtered:
            buy_count = sum(1 for b in bias_list if b == "BUY")
            sell_count = sum(1 for b in bias_list if b == "SELL")
            none_count = sum(1 for b in bias_list if b is None)
            total = len(bias_list)
            per_strategy_bias[s_name] = {
                "active": True,
                "filters": s_bias_cfg.get("bias_filters", []),
                "min_agreement": s_bias_cfg.get("min_agreement", 2),
                "buy_bars": buy_count,
                "sell_bars": sell_count,
                "neutral_bars": none_count,
                "total_bars": total,
                "buy_pct": round(buy_count / max(total, 1) * 100, 1),
                "sell_pct": round(sell_count / max(total, 1) * 100, 1),
            }
        else:
            per_strategy_bias[s_name] = {"active": False}

    # Slot configs (for deploy button — frontend needs the full config)
    slot_configs = []
    for s in slots:
        slot_configs.append({
            "strategy_name": s["name"],
            "effective_name": s["name"],
            "capital_allocation": 100_000,
            "params": s.get("params", {}),
            "bias_config": s.get("bias_config"),
            "session": s.get("session", "all"),
            "max_fires_per_day": s.get("max_fires_per_day", 5),
            "time_stop_bars": s.get("time_stop_bars", 20),
            "exit_config": {
                "sl_atr_mult":   s.get("sl_atr_mult",   exit_cfg["sl_atr_mult"]),
                "tp_atr_mult":   s.get("tp_atr_mult",   exit_cfg["tp_atr_mult"]),
                "max_hold_bars": s.get("max_hold_bars",  exit_cfg["max_hold_bars"]),
                "slippage_pts":  exit_cfg["slippage_pts"],
            },
        })

    from .reporting import build_full_result
    raw = {
        "trades": all_trades, "equity_curve": equity_snapshots,
        "per_strategy_equity": {}, "per_strategy_trades": per_strat_trades,
        "strategy_names": strategy_names,
        "start_ts": actual_start_ts,
        "end_ts": float(timestamps[-1]) if len(timestamps) > 0 else 0,
        "initial_capital": 500_000,
    }
    cfg_dict = {"strategies": slot_configs}
    result = build_full_result(raw, cfg_dict)
    result["per_strategy_bias"] = per_strategy_bias
    result["slot_configs"] = slot_configs
    result["instrument"] = instrument
    return result


# ── Inline helpers ────────────────────────────────────────────────────────────

def _check_exit_fast(ot, high, low, price, t_min, i, exit_cfg, force_exit_min):
    held = i - ot["entry_bar"]
    d = ot["direction"]
    sl_hit = (low <= ot["sl"]) if d == "BUY" else (high >= ot["sl"])
    tp_hit = (high >= ot["tp"]) if d == "BUY" else (low <= ot["tp"])
    if sl_hit and tp_hit:
        return "stop_loss", ot["sl"]
    if tp_hit:
        return "target", ot["tp"]
    if sl_hit:
        return "stop_loss", ot["sl"]
    if held >= ot.get("max_hold", exit_cfg["max_hold_bars"]):
        return "time_stop", float(price)
    if t_min >= force_exit_min:
        return "square_off", float(price)
    return None, None


def _close_trade(ot, reason, exit_price, i, ts, exit_cfg, lot_size, all_trades):
    pnl_pts = (exit_price - ot["entry_price"]) if ot["direction"] == "BUY" else (ot["entry_price"] - exit_price)
    pnl_pts -= exit_cfg["slippage_pts"]
    all_trades.append({
        "strategy_name": ot["strategy"],
        "direction": 1 if ot["direction"] == "BUY" else -1,
        "entry_price": round(ot["entry_price"], 2),
        "exit_price": round(exit_price, 2),
        "stop_loss": round(ot["sl"], 2), "target": round(ot["tp"], 2),
        "hold_candles": i - ot["entry_bar"],
        "exit_bar_ts": float(ts), "quantity": 1, "lot_size": lot_size,
        "pnl": round(pnl_pts * lot_size, 2),
        "slippage": exit_cfg["slippage_pts"], "brokerage": 20.0,
        "exit_reason": reason, "tag": ot["direction"],
    })
    return pnl_pts


def _empty_result() -> dict:
    return {
        "metrics": {}, "per_strategy_metrics": {}, "equity_curve": [],
        "per_strategy_equity": {}, "trades": [], "monthly_pnl": [],
        "daily_pnl": [], "strategy_names": [], "initial_capital": 0,
        "start_ts": 0, "end_ts": 0,
    }
