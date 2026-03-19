"""
BacktestRunner — top-level orchestrator.

1. Validates config
2. Instantiates strategy adapters
3. Attempts to use Rust engine; falls back to Python engine
4. Returns full result dict
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from .adapter import LegacyStrategyAdapter
from .config import BacktestConfig
from .reporting import build_full_result

# ── Strategy Registry Import ───────────────────────────────────────────────────

def _get_strategy_registry() -> dict:
    """Import STRATEGY_REGISTRY from user_worker_pool if available."""
    try:
        # Running inside the Resolute monorepo
        from services.user_worker_pool.strategies import STRATEGY_REGISTRY
        return STRATEGY_REGISTRY
    except ImportError:
        pass
    try:
        # Running from backtest/ directory directly
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from services.user_worker_pool.strategies import STRATEGY_REGISTRY
        return STRATEGY_REGISTRY
    except ImportError:
        return {}


# ── Rust Engine Import ─────────────────────────────────────────────────────────

def _try_import_rust_engine():
    """Try to import the compiled Rust extension. Returns None if not built."""
    try:
        import backtest_engine as _eng
        return _eng
    except ImportError:
        return None


# ── Python Fallback Engine ─────────────────────────────────────────────────────

def _run_python_engine(
    config: BacktestConfig,
    adapters: list[LegacyStrategyAdapter],
    strategy_engine_cfgs: list[dict],
) -> dict:
    """Pure Python fallback (slower but always available)."""
    import json
    import os
    from datetime import datetime, timezone, timedelta

    data_dir = config.data_dir
    instruments = config.instruments
    start_ts = config.warmup_start_ts()
    end_ts = config.end_ts()
    actual_start_ts = config.start_ts()

    # Load 1m candles for each instrument
    combined_1m = {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
    ist = timezone(timedelta(hours=5, minutes=30))

    for instrument in instruments:
        inst_dir = data_dir / instrument
        if not inst_dir.exists():
            continue
        files = sorted(inst_dir.glob("*_1m.json"))
        for f in files:
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            ts_arr = data.get("timestamp", [])
            for i, ts in enumerate(ts_arr):
                if ts < start_ts or ts > end_ts:
                    continue
                combined_1m["open"].append(data["open"][i])
                combined_1m["high"].append(data["high"][i])
                combined_1m["low"].append(data["low"][i])
                combined_1m["close"].append(data["close"][i])
                combined_1m["volume"].append(data.get("volume", [0.0] * len(ts_arr))[i])
                combined_1m["timestamp"].append(ts)

    # Sort by timestamp
    import numpy as np
    for k in combined_1m:
        combined_1m[k] = np.array(combined_1m[k], dtype=np.float64)

    if len(combined_1m["close"]) == 0:
        return {"trades": [], "equity_curve": [], "per_strategy_equity": {}, "per_strategy_trades": {},
                "strategy_names": [a.strategy.name for a in adapters],
                "start_ts": actual_start_ts, "end_ts": end_ts, "initial_capital": config.initial_capital}

    # Sort
    order = np.argsort(combined_1m["timestamp"])
    for k in combined_1m:
        combined_1m[k] = combined_1m[k][order]

    # Build TF aggregations
    from .data_utils import aggregate_numpy
    tf_set = set()
    for cfg in strategy_engine_cfgs:
        tf_set.add(cfg["primary_tf_minutes"])

    candles_tf = {1: combined_1m}
    for tf in tf_set:
        if tf != 1:
            candles_tf[tf] = aggregate_numpy(combined_1m, tf)

    # Build TF close map
    from .data_utils import build_tf_close_map
    tf_close_maps = {tf: build_tf_close_map(combined_1m, tf) for tf in tf_set}

    # Build 1m→TF index map
    from .data_utils import build_1m_to_tf_index
    tf_index_maps = {tf: build_1m_to_tf_index(combined_1m, tf) for tf in tf_set}

    n_bars = len(combined_1m["close"])
    n_strategies = len(adapters)
    IST_OFFSET = 330 * 60

    # Pre-compute default close maps to avoid creating [False]*n_bars every bar
    _default_close = np.zeros(n_bars, dtype=bool)
    _prebuilt_tf_close = {tf: np.array(tf_close_maps.get(tf, _default_close), dtype=bool)
                          for tf in {c["primary_tf_minutes"] for c in strategy_engine_cfgs}}

    # Portfolio state
    portfolios = [{"capital": cfg["capital_allocation"], "peak": cfg["capital_allocation"],
                   "daily_pnl": 0.0, "trades": 0, "killed": False}
                  for cfg in strategy_engine_cfgs]
    open_positions: list[list[dict]] = [[] for _ in range(n_strategies)]
    all_trades: list[dict] = []
    per_strategy_trades: list[list[dict]] = [[] for _ in range(n_strategies)]
    equity_snapshots: list[dict] = []
    per_strategy_equity: list[list[dict]] = [[] for _ in range(n_strategies)]
    pos_id = 0
    prev_day = -1
    # Only emit equity snapshot every 5 bars (reduces 24k → 5k dicts for 3m data)
    EQUITY_SAMPLE_EVERY = 5

    for bar_idx in range(n_bars):
        ts = float(combined_1m["timestamp"][bar_idx])
        ist_ts = int(ts) + IST_OFFSET
        day = ist_ts // 86400
        ist_min = (ist_ts % 86400) // 60

        if day != prev_day:
            for p in portfolios:
                p["daily_pnl"] = 0.0
                p["trades"] = 0
            prev_day = day

        bar_high = float(combined_1m["high"][bar_idx])
        bar_low = float(combined_1m["low"][bar_idx])
        bar_close = float(combined_1m["close"][bar_idx])

        for s_idx, cfg in enumerate(strategy_engine_cfgs):
            port = portfolios[s_idx]
            if port["killed"]:
                continue

            # Check stops/targets on open positions
            closed_ids = []
            for pos in open_positions[s_idx]:
                sl_hit = (bar_low <= pos["stop_loss"] if pos["direction"] == 1
                          else bar_high >= pos["stop_loss"])
                tgt_hit = (bar_high >= pos["target"] if pos["direction"] == 1
                           else bar_low <= pos["target"])
                time_hit = bar_idx >= pos["time_stop_bar"]

                exit_price, reason = None, None
                if time_hit:
                    exit_price, reason = bar_close, "time_stop"
                elif sl_hit and tgt_hit:
                    exit_price, reason = pos["stop_loss"], "stop_loss"  # worst case
                elif sl_hit:
                    exit_price, reason = pos["stop_loss"], "stop_loss"
                elif tgt_hit:
                    exit_price, reason = pos["target"], "target"

                if exit_price is not None:
                    pnl = (exit_price - pos["entry_price"]) * pos["direction"] * pos["quantity"] * pos["lot_size"]
                    port["capital"] += pnl
                    port["daily_pnl"] += pnl
                    if port["capital"] > port["peak"]:
                        port["peak"] = port["capital"]
                    rec = {"strategy_name": cfg["name"], "direction": pos["direction"],
                           "entry_price": pos["entry_price"], "exit_price": exit_price,
                           "stop_loss": pos["stop_loss"], "target": pos["target"],
                           "hold_candles": bar_idx - pos.get("entry_bar", bar_idx),
                           "exit_bar_ts": ts, "quantity": pos["quantity"], "lot_size": pos["lot_size"],
                           "pnl": round(pnl, 2), "slippage": 0.5, "brokerage": 20.0,
                           "exit_reason": reason, "tag": pos["tag"]}
                    all_trades.append(rec)
                    per_strategy_trades[s_idx].append(rec)
                    closed_ids.append(pos["id"])

            open_positions[s_idx] = [p for p in open_positions[s_idx] if p["id"] not in closed_ids]

            # Only evaluate on primary TF bar close
            if not _prebuilt_tf_close[cfg["primary_tf_minutes"]][bar_idx]:
                continue

            # Skip if outside actual backtest range (warm-up bars)
            if ts < actual_start_ts:
                continue

            # Session window
            if ist_min < cfg["active_start_minutes"] or ist_min > cfg["active_end_minutes"]:
                continue

            # Square-off
            if ist_min >= cfg["square_off_minutes"]:
                for pos in open_positions[s_idx]:
                    pnl = (bar_close - pos["entry_price"]) * pos["direction"] * pos["quantity"] * pos["lot_size"]
                    port["capital"] += pnl
                    port["daily_pnl"] += pnl
                    if port["capital"] > port["peak"]:
                        port["peak"] = port["capital"]
                    rec = {"strategy_name": cfg["name"], "direction": pos["direction"],
                           "entry_price": pos["entry_price"], "exit_price": bar_close,
                           "stop_loss": pos["stop_loss"], "target": pos["target"],
                           "hold_candles": bar_idx - pos.get("entry_bar", bar_idx),
                           "exit_bar_ts": ts, "quantity": pos["quantity"], "lot_size": pos["lot_size"],
                           "pnl": round(pnl, 2), "slippage": 0.0, "brokerage": 20.0,
                           "exit_reason": "square_off", "tag": pos["tag"]}
                    all_trades.append(rec)
                    per_strategy_trades[s_idx].append(rec)
                open_positions[s_idx] = []
                continue

            # Max positions
            if len(open_positions[s_idx]) >= cfg["max_positions"]:
                continue

            # Drawdown kill — disabled for index-point backtesting
            # (goal is to capture all signals, not simulate capital management)

            # Call strategy
            tf = cfg["primary_tf_minutes"]
            tf_idx = tf_index_maps.get(tf, list(range(n_bars)))[bar_idx]
            tf_candles = candles_tf.get(tf, combined_1m)

            try:
                signal = adapters[s_idx].evaluate_bar(
                    bar_idx, tf_idx, combined_1m, tf_candles, open_positions[s_idx]
                )
            except Exception:
                continue

            if signal is None:
                continue

            # Fill on next bar open
            fill_bar = min(bar_idx + 1, n_bars - 1)
            fill_price = float(combined_1m["open"][fill_bar]) + signal.get("entry_price", 0) * 0.0
            # Use signal entry_price if > 0, else next bar open
            ep = signal.get("entry_price", 0.0)
            if ep and ep > 0:
                fill_price = ep

            lot_size = cfg["lot_size"]
            pos_id += 1
            open_positions[s_idx].append({
                "id": pos_id,
                "direction": signal["direction"],
                "entry_price": fill_price,
                "stop_loss": signal["stop_loss"],
                "target": signal["target"],
                "quantity": signal.get("quantity", 1),
                "lot_size": lot_size,
                "time_stop_bar": bar_idx + signal.get("time_stop_bars", 120),
                "entry_bar": bar_idx,
                "tag": signal.get("tag", ""),
            })

        # Equity snapshot (sampled every N bars to avoid 24k dict allocations)
        if bar_idx % EQUITY_SAMPLE_EVERY == 0 or bar_idx == n_bars - 1:
            total_eq = sum(p["capital"] for p in portfolios)
            total_peak = sum(p["peak"] for p in portfolios)
            dd_pct = (total_peak - total_eq) / total_peak * 100.0 if total_peak > 0 else 0.0
            equity_snapshots.append({"timestamp": ts, "equity": total_eq, "drawdown_pct": dd_pct})
            for s_idx, p in enumerate(portfolios):
                dd = (p["peak"] - p["capital"]) / p["peak"] * 100.0 if p["peak"] > 0 else 0.0
                per_strategy_equity[s_idx].append({"timestamp": ts, "equity": p["capital"], "drawdown_pct": dd})

    strategy_names = [cfg["name"] for cfg in strategy_engine_cfgs]
    per_strategy_equity_named = {name: per_strategy_equity[i] for i, name in enumerate(strategy_names)}
    per_strategy_trades_named = {name: per_strategy_trades[i] for i, name in enumerate(strategy_names)}

    return {
        "trades": all_trades,
        "equity_curve": equity_snapshots,
        "per_strategy_equity": per_strategy_equity_named,
        "per_strategy_trades": per_strategy_trades_named,
        "strategy_names": strategy_names,
        "start_ts": actual_start_ts,
        "end_ts": end_ts,
        "initial_capital": config.initial_capital,
    }


# ── Main Runner ────────────────────────────────────────────────────────────────

def run(config: BacktestConfig) -> dict:
    """
    Run a full backtest.

    Returns a complete result dict with metrics, equity curves, trades, etc.
    """
    registry = _get_strategy_registry()

    # Instantiate strategy adapters
    adapters = []
    strategy_engine_cfgs = []
    primary_instrument = config.instruments[0] if config.instruments else "NIFTY_50"

    for s_cfg in config.strategies:
        strategy_cls = registry.get(s_cfg.strategy_name)
        if strategy_cls is None:
            raise ValueError(f"Unknown strategy: {s_cfg.strategy_name!r}. "
                             f"Available: {sorted(registry.keys())}")

        strategy_inst = strategy_cls()
        adapter = LegacyStrategyAdapter(
            strategy=strategy_inst,
            config=s_cfg.params,
            instrument=primary_instrument,
            primary_tf=s_cfg.primary_timeframe,
            max_hold_bars=getattr(s_cfg, 'max_hold_bars', 20),
        )
        adapters.append(adapter)
        eng_dict = s_cfg.to_engine_dict(config.lot_sizes, primary_instrument)
        # Clamp TF to minimum 5m — strategies use 5m candles, no need for
        # per-1m-bar callbacks (saves 80% of PyO3 call overhead)
        if eng_dict["primary_tf_minutes"] < 5:
            eng_dict["primary_tf_minutes"] = 5
        strategy_engine_cfgs.append(eng_dict)

    # Try Rust engine first
    rust_engine = _try_import_rust_engine()
    if rust_engine is not None:
        try:
            raw = rust_engine.run_backtest(
                str(config.data_dir),
                config.instruments,
                config.warmup_start_ts(),
                config.end_ts(),
                adapters,
                strategy_engine_cfgs,
                config.to_engine_dict(),
            )
        except Exception as e:
            # Fall back to Python engine on Rust error
            print(f"[backtest] Rust engine error ({e}), falling back to Python engine")
            raw = _run_python_engine(config, adapters, strategy_engine_cfgs)
    else:
        raw = _run_python_engine(config, adapters, strategy_engine_cfgs)

    # Filter equity/trades to actual backtest range (strip warm-up)
    actual_start = config.start_ts()
    raw["equity_curve"] = [s for s in raw["equity_curve"] if s["timestamp"] >= actual_start]
    raw["trades"] = [t for t in raw["trades"] if t.get("exit_bar_ts", 0) >= actual_start]

    # Build full result with metrics
    config_dict = {
        "strategies": [
            {"strategy_name": sc.strategy_name,
             "effective_name": sc.effective_name,
             "capital_allocation": sc.capital_allocation}
            for sc in config.strategies
        ]
    }
    return build_full_result(raw, config_dict)
