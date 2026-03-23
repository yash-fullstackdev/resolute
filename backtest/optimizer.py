"""Fast parameter optimizer — loads data ONCE, varies only strategy params.

Instead of 144 × full multi_runner.run() calls (~3s each = 432s),
this loads data once (~3s), then runs 144 lightweight signal+exit sweeps (~0.02s each = ~3s).
Total: ~6s instead of ~432s. Same results.
"""

from __future__ import annotations

import itertools
import time
from collections import defaultdict
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_S = 19800
SL_CAPS = {"NIFTY_50": 20, "BANK_NIFTY": 40, "SENSEX": 60, "NIFTY": 20, "BANKNIFTY": 40}


def run_optimization(config: dict) -> dict:
    """Run fast parameter grid optimization.

    Config:
        instrument, start_date, end_date, data_dir,
        strategy_name, param_grid, exit_config,
        bias_config (optional), optimize_for, session
    """
    from .multi_runner import _load_1m_data
    from .fast_strategies import precompute_strategy_signals
    from services.user_worker_pool.bias.evaluator import BiasEvaluator, aggregate_candles, atr_full

    instrument = config.get("instrument", "NIFTY_50")
    start_date = config.get("start_date", "2025-01-01")
    end_date = config.get("end_date", "2025-12-31")
    data_dir = Path(config.get("data_dir", "/data"))
    strategy_name = config.get("strategy_name", "ttm_squeeze")
    param_grid = config.get("param_grid", {})
    # Per-strategy exit defaults — single source of truth in fast_strategies.py
    from .fast_strategies import get_strategy_exit_defaults
    _default_exit = {**get_strategy_exit_defaults(strategy_name), "slippage_pts": 0.5}
    exit_cfg = config.get("exit_config", _default_exit)
    bias_cfg = config.get("bias_config") or {}
    optimize_for = config.get("optimize_for", "profit_factor")
    session = config.get("session", "all")

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    if not combinations:
        return {"strategy_name": strategy_name, "instrument": instrument,
                "optimize_for": optimize_for, "total_combinations": 0,
                "best": None, "results": []}

    # ── Load data ONCE ────────────────────────────────────────────────
    data_1m = _load_1m_data(data_dir, instrument, start_date, end_date)
    if not data_1m or len(data_1m.get("close", [])) == 0:
        return {"strategy_name": strategy_name, "instrument": instrument,
                "optimize_for": optimize_for, "total_combinations": len(combinations),
                "best": None, "results": []}

    for k in ("open", "high", "low", "close", "volume", "timestamp"):
        if not isinstance(data_1m[k], np.ndarray):
            data_1m[k] = np.array(data_1m[k], dtype=np.float64)

    timestamps = data_1m["timestamp"]
    closes = data_1m["close"]
    highs = data_1m["high"]
    lows = data_1m["low"]
    n = len(closes)

    # Build 5m candles ONCE
    data_5m = aggregate_candles(data_1m, 300)
    ts_5m = data_5m.get("timestamp", np.array([]))
    n_5m = len(data_5m.get("close", []))
    if n_5m < 15:
        return {"strategy_name": strategy_name, "instrument": instrument,
                "optimize_for": optimize_for, "total_combinations": len(combinations),
                "best": None, "results": []}

    # Build 15m candles ONCE (required by brahmaastra, ema5_mean_reversion)
    data_15m = aggregate_candles(data_1m, 900)
    n_15m = len(data_15m.get("close", []))

    # Build 1H candles ONCE (required by parent_child_momentum parent timeframe)
    data_1h = aggregate_candles(data_1m, 3600)
    n_1h = len(data_1h.get("close", []))

    # ATR ONCE
    atr_arr = atr_full(data_5m["high"], data_5m["low"], data_5m["close"], 14)
    atr_cache = np.zeros(n_5m)
    for i in range(len(atr_arr)):
        if 14 + i < n_5m:
            atr_cache[14 + i] = atr_arr[i]

    # Bias — pre-compute once. If test_bias_on_off, we'll test both with and without.
    test_bias_on_off = config.get("test_bias_on_off", False)
    bias_cache_on = [None] * n_5m
    if bias_cfg.get("bias_filters") and bias_cfg.get("mode") == "bias_filtered":
        evaluator = BiasEvaluator(bias_cfg)
        bias_cache_on, _ = evaluator.precompute_bias_array(data_1m)
    bias_cache_off = [None] * n_5m  # all None = no filtering

    bias_variants = [("bias_on", bias_cache_on)] if bias_cfg.get("mode") == "bias_filtered" else [("no_bias", bias_cache_off)]
    if test_bias_on_off and bias_cfg.get("bias_filters"):
        bias_variants = [("bias_on", bias_cache_on), ("no_bias", bias_cache_off)]

    # Pre-compute 5m close indices ONCE
    from .multi_runner import _build_5m_close_set
    five_m_set = _build_5m_close_set(timestamps, ts_5m)
    five_m_indices = sorted(idx for idx in five_m_set if idx >= int(np.searchsorted(
        timestamps, datetime(_date.fromisoformat(start_date).year,
                             _date.fromisoformat(start_date).month,
                             _date.fromisoformat(start_date).day, 9, 0, tzinfo=IST).timestamp()
    )))

    # IST minutes ONCE
    ist_secs = (timestamps.astype(np.int64) + IST_OFFSET_S)
    all_t_min = ((ist_secs % 86400) // 60).astype(np.int32)
    all_day_num = (ist_secs // 86400).astype(np.int32)

    # Session bounds — loaded from shared config (same source as multi_runner)
    from .fast_strategies import get_strategy_session
    _sess           = get_strategy_session(strategy_name)
    entry_start     = _sess["entry_start"]
    morning_end     = 690                    # 11:30 IST — used for non-unified split
    afternoon_start = 780                    # 13:00 IST
    entry_cutoff    = _sess["entry_cutoff"]
    force_exit_min  = _sess["force_exit_min"]
    max_fires_day   = _sess["max_fires"]
    unified_window  = _sess["unified_window"]

    sl_cap = SL_CAPS.get(instrument, 30)
    lot_size = {"NIFTY_50": 75, "BANK_NIFTY": 30, "SENSEX": 10, "NIFTY": 75, "BANKNIFTY": 30}.get(instrument, 50)

    slippage = float(exit_cfg.get("slippage_pts", 0.5))

    # ── Exit param grid (sweep SL/TP/MaxHold combinations) ──────────
    exit_grid = config.get("exit_grid", {})
    _sl_vals  = exit_grid.get("sl_atr_mult",  [float(exit_cfg.get("sl_atr_mult",  0.5))])
    _tp_vals  = exit_grid.get("tp_atr_mult",  [float(exit_cfg.get("tp_atr_mult",  1.5))])
    _mh_vals  = exit_grid.get("max_hold_bars", [int(exit_cfg.get("max_hold_bars", 20))])
    exit_combos = [
        {"sl_atr_mult": sl, "tp_atr_mult": tp, "max_hold_bars": mh}
        for sl, tp, mh in itertools.product(_sl_vals, _tp_vals, _mh_vals)
    ]

    # ── Separate signal params from exit params ─────────────────────
    # Signal params affect which bars generate signals (need recomputation)
    # Exit params only affect SL/TP/hold (cheap to vary)
    EXIT_ONLY_KEYS = {"max_sl_points", "max_fires_per_day", "time_stop_bars"}
    signal_keys = [k for k in keys if k not in EXIT_ONLY_KEYS]
    exit_keys = [k for k in keys if k in EXIT_ONLY_KEYS]

    # Pre-build prev-day arrays for Brahmaastra (data-derived, not hyper-params)
    _brahmaastra_prev_day: dict = {}
    if strategy_name == "brahmaastra" and n_15m > 0:
        from .fast_strategies import build_prev_day_arrays
        pdc_arr, pdh_arr, pdl_arr = build_prev_day_arrays(
            data_1m["close"], data_1m["high"], data_1m["low"],
            data_1m["timestamp"], data_15m["timestamp"],
        )
        _brahmaastra_prev_day = {"pdc_arr": pdc_arr, "pdh_arr": pdh_arr, "pdl_arr": pdl_arr}

    # Group by signal params — compute signals once per unique signal config
    signal_combos: dict[tuple, np.ndarray] = {}
    opens_5m = data_5m.get("open", data_5m["close"])

    for combo in combinations:
        params_full = dict(zip(keys, combo))
        sig_key = tuple(params_full.get(k, 0) for k in signal_keys)

        if sig_key not in signal_combos:
            sig_params = {k: float(params_full[k]) for k in signal_keys if k in params_full}
            sig_params.update(_brahmaastra_prev_day)  # injects pdc/pdh/pdl for brahmaastra, no-op otherwise
            signals = precompute_strategy_signals(
                strategy_name,
                data_5m["close"], data_5m["high"], data_5m["low"],
                opens_5m, sig_params,
                # Multi-timeframe arrays — only consumed by strategies that need them
                closes_15m=data_15m.get("close") if n_15m > 0 else None,
                highs_15m=data_15m.get("high")   if n_15m > 0 else None,
                lows_15m=data_15m.get("low")      if n_15m > 0 else None,
                opens_15m=data_15m.get("open")    if n_15m > 0 else None,
                timestamps_15m=data_15m.get("timestamp") if n_15m > 0 else None,
                closes_1h=data_1h.get("close")   if n_1h  > 0 else None,
            )
            if signals is not None:
                signal_combos[sig_key] = signals

    # ── Run each param combo × bias variant ─────────────────────────
    results = []
    best = None
    best_score = float("-inf")

    for combo in combinations:
      for exit_combo in exit_combos:
        sl_atr_mult = float(exit_combo["sl_atr_mult"])
        tp_atr_mult = float(exit_combo["tp_atr_mult"])
        max_hold    = int(exit_combo["max_hold_bars"])
        for bias_label, bias_cache in bias_variants:
          params = {k: float(v) for k, v in zip(keys, combo)}
          sig_key = tuple(params.get(k, 0) for k in signal_keys)
          signals = signal_combos.get(sig_key)
          if signals is None:
              continue

          strategy_sl_cap = params.get("max_sl_points", sl_cap)

          # Walk-forward with this signal array
          all_pnls = []
          daily_pnl: dict[int, float] = defaultdict(float)
          prev_day = -1
          daily_fires = 0
          trade_exit_bar = -1

          for i in five_m_indices:
            t_min = int(all_t_min[i])
            day_num = int(all_day_num[i])

            if day_num != prev_day:
                prev_day = day_num
                daily_fires = 0

            # Session filter — uses shared STRATEGY_SESSION_CONFIG via get_strategy_session()
            if unified_window:
                if not (entry_start <= t_min <= entry_cutoff):
                    continue
            else:
                is_morning   = entry_start <= t_min <= morning_end
                is_afternoon = afternoon_start <= t_min <= entry_cutoff
                if session == "morning" and not is_morning:
                    continue
                if session == "afternoon" and not is_afternoon:
                    continue
                if not (is_morning or is_afternoon):
                    continue

            if daily_fires >= max_fires_day:
                continue
            if i <= trade_exit_bar:
                continue

            n5 = int(np.searchsorted(ts_5m, timestamps[i] - 300, side="right"))
            if n5 < 15 or n5 - 1 >= len(signals):
                continue

            atr = atr_cache[n5 - 1]
            if atr <= 0:
                continue

            # Bias check (uses current bias variant)
            bias = bias_cache[n5 - 1] if n5 - 1 < len(bias_cache) else None
            if bias_label == "bias_on" and not bias:
                continue

            sig_val = int(signals[n5 - 1])
            if sig_val == 0:
                continue
            sig_dir = "BUY" if sig_val == 1 else "SELL"

            if bias_label == "bias_on" and bias and sig_dir != bias:
                continue

            price = float(closes[i])
            entry = price + (slippage if sig_dir == "BUY" else -slippage)
            sl_dist = min(sl_atr_mult * atr, strategy_sl_cap)
            tp_dist = tp_atr_mult * atr

            if sig_dir == "BUY":
                sl, tp = entry - sl_dist, entry + tp_dist
            else:
                sl, tp = entry + sl_dist, entry - tp_dist

            # Find exit (vectorized)
            end_bar = min(i + max_hold + 1, n)
            if i + 1 >= n:
                continue

            h_slice = highs[i + 1:end_bar]
            l_slice = lows[i + 1:end_bar]
            tmin_slice = all_t_min[i + 1:end_bar]

            if len(h_slice) == 0:
                continue

            if sig_dir == "BUY":
                sl_hits = np.where(l_slice <= sl)[0]
                tp_hits = np.where(h_slice >= tp)[0]
            else:
                sl_hits = np.where(h_slice >= sl)[0]
                tp_hits = np.where(l_slice <= tp)[0]

            force_hits = np.where(tmin_slice >= force_exit_min)[0]

            sl_bar = int(sl_hits[0]) if len(sl_hits) > 0 else len(h_slice) + 1
            tp_bar = int(tp_hits[0]) if len(tp_hits) > 0 else len(h_slice) + 1
            force_bar = int(force_hits[0]) if len(force_hits) > 0 else len(h_slice) + 1
            time_bar = max_hold

            min_bar = min(sl_bar, tp_bar, force_bar, time_bar)

            if min_bar >= len(h_slice):
                exit_price = float(closes[min(i + max_hold, n - 1)])
            elif tp_bar <= sl_bar and tp_bar == min_bar:
                exit_price = float(tp)
            elif sl_bar == min_bar:
                exit_price = float(sl)
            else:
                exit_price = float(closes[min(i + 1 + min_bar, n - 1)])

            pnl_pts = (exit_price - entry) if sig_dir == "BUY" else (entry - exit_price)
            pnl_pts -= slippage
            all_pnls.append(pnl_pts)

            trade_exit_bar = i + 1 + min(min_bar, len(h_slice) - 1)
            daily_fires += 1

          # Compute metrics (inside bias_label loop)
          total_trades = len(all_pnls)
          if total_trades < 5:
              continue

          wins = [p for p in all_pnls if p > 0]
          losses = [p for p in all_pnls if p < 0]
          total_pnl = sum(all_pnls)
          win_rate = len(wins) / total_trades * 100
          gross_win = sum(wins) if wins else 0
          gross_loss = abs(sum(losses)) if losses else 0
          profit_factor = gross_win / gross_loss if gross_loss > 0 else 9999

          # Sharpe (daily returns approximation)
          if len(all_pnls) > 1:
              arr = np.array(all_pnls)
              sharpe = float(np.mean(arr) / np.std(arr) * np.sqrt(252)) if np.std(arr) > 0 else 0
          else:
              sharpe = 0

          # Drawdown
          cum = np.cumsum(all_pnls)
          peak = np.maximum.accumulate(cum)
          dd = peak - cum
          max_dd = float(np.max(dd)) if len(dd) > 0 else 0
          max_dd_pct = max_dd / max(abs(float(np.max(peak))), 1) * 100 if len(peak) > 0 else 0

          score_map = {
              "sharpe": sharpe,
              "profit_factor": profit_factor if profit_factor < 9999 else 0,
              "total_pnl": total_pnl,
              "win_rate": win_rate,
          }
          score = score_map.get(optimize_for, 0)

          # Include exit params in result so UI can show them
          entry_result = {
              "params": {**params, "sl_atr_mult": sl_atr_mult, "tp_atr_mult": tp_atr_mult, "max_hold_bars": max_hold},
              "bias": bias_label,
              "total_trades": total_trades,
              "win_rate": round(win_rate, 1),
              "profit_factor": round(min(profit_factor, 9999), 2),
              "sharpe": round(sharpe, 3),
              "total_pnl": round(total_pnl, 1),
              "max_drawdown": round(max_dd_pct, 1),
              "score": round(score, 3),
          }
          results.append(entry_result)

          if score > best_score and total_trades >= 10:
              best_score = score
              best = entry_result

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "strategy_name": strategy_name,
        "instrument": instrument,
        "optimize_for": optimize_for,
        "total_combinations": len(combinations) * len(exit_combos),
        "best": best,
        "results": results[:50],
    }
