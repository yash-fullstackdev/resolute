"""Worker pool bootstrap — starts strategy workers for all users on app startup.

Loads all tenants with enabled strategy instances from DB,
spawns a background worker for each tenant that:
  1. Warms up candle history from Dhan API
  2. Subscribes to live ticks via NATS
  3. Evaluates strategies every minute
  4. Publishes signals (paper or live)

Workers run as asyncio tasks — they don't block the HTTP server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from .db import rls_session
from sqlalchemy import text

logger = structlog.get_logger(service="dashboard_api", module="worker_bootstrap")


class SimpleWorkerPool:
    """Lightweight worker pool — spawns a candle+strategy worker per tenant."""

    def __init__(self):
        self.workers: dict[str, Any] = {}  # tenant_id → worker instance
        self._tasks: dict[str, asyncio.Task] = {}

    async def spawn_for_tenant(self, tenant_id: str, nats_client, instances: list[dict]):
        """Spawn a worker for one tenant."""
        if tenant_id in self.workers:
            return

        try:
            from services.user_worker_pool.candles.store import CandleStore
            from services.user_worker_pool.bias.evaluator import BiasEvaluator
            from services.user_worker_pool.strategies import STRATEGY_REGISTRY

            # Collect all instruments across instances
            all_instruments = set()
            for inst in instances:
                all_instruments.update(inst.get("instruments", []))

            # Build candle store and warmup
            candle_store = CandleStore()
            if all_instruments:
                await candle_store.warmup(list(all_instruments))
                candle_store.set_poll_symbols(list(all_instruments))

            # Build strategy instances
            from services.user_worker_pool.pool.worker import (
                StrategyInstance, _normalize_symbol, _symbol_matches,
            )

            strategy_instances = []
            for inst_cfg in instances:
                mode = inst_cfg.get("mode", "disabled")
                if mode == "disabled":
                    continue

                strategy_name = inst_cfg["strategy_name"]
                cls = STRATEGY_REGISTRY.get(strategy_name)
                if cls is None:
                    continue

                # Build bias evaluator
                bias_eval = None
                bias_cfg = inst_cfg.get("bias_config")
                if bias_cfg and bias_cfg.get("mode") == "bias_filtered":
                    bias_eval = BiasEvaluator(bias_cfg)
                    if not bias_eval.is_active:
                        bias_eval = None

                # Merge exit_config into params so worker can access sl_atr_mult etc.
                merged_config = dict(inst_cfg.get("params", {}))
                exit_cfg = merged_config.pop("exit_config", None)
                if isinstance(exit_cfg, dict):
                    merged_config.update(exit_cfg)

                strategy_instances.append(StrategyInstance(
                    instance_id=inst_cfg["instance_id"],
                    instance_name=inst_cfg["instance_name"],
                    strategy=cls(),
                    config=merged_config,
                    instruments=inst_cfg.get("instruments", []),
                    bias_evaluator=bias_eval,
                    session=inst_cfg.get("session", "all"),
                    mode=mode,
                    max_daily_loss_pts=inst_cfg.get("max_daily_loss_pts"),
                ))

            if not strategy_instances:
                return

            # Create a lightweight worker
            worker = _TenantWorker(
                tenant_id=tenant_id,
                candle_store=candle_store,
                instances=strategy_instances,
                nats_client=nats_client,
            )
            self.workers[tenant_id] = worker

            # Run as background task
            task = asyncio.create_task(worker.run(), name=f"worker_{tenant_id}")
            self._tasks[tenant_id] = task

            logger.info(
                "worker_spawned",
                tenant_id=tenant_id,
                instances=len(strategy_instances),
                instruments=sorted(all_instruments),
            )

        except Exception as exc:
            logger.error("worker_spawn_failed", tenant_id=tenant_id, error=str(exc))

    async def reload_tenant(self, tenant_id: str, nats_client):
        """Stop and respawn worker for a tenant (called on config change)."""
        # Stop existing worker
        if tenant_id in self.workers:
            self.workers[tenant_id].stop()
            del self.workers[tenant_id]
        if tenant_id in self._tasks:
            self._tasks[tenant_id].cancel()
            del self._tasks[tenant_id]

        # Reload instances from DB
        try:
            async with rls_session(tenant_id) as session:
                result = await session.execute(text("""
                    SELECT id, strategy_name, instance_name, params, enabled,
                           trading_mode, session, max_daily_loss_pts
                    FROM user_strategy_configs
                    WHERE tenant_id = :tid AND enabled = TRUE
                          AND trading_mode IN ('live', 'paper')
                    ORDER BY strategy_name
                """), {"tid": tenant_id})

                instances = []
                for row in result.mappings().all():
                    raw_params = row["params"] if isinstance(row["params"], dict) else {}
                    instruments = raw_params.pop("instruments", [])
                    bias_config = raw_params.pop("bias_config", None)
                    instances.append({
                        "instance_id": str(row["id"]),
                        "instance_name": row["instance_name"] or row["strategy_name"],
                        "strategy_name": row["strategy_name"],
                        "mode": row["trading_mode"] or "paper",
                        "session": row["session"] or "all",
                        "max_daily_loss_pts": row["max_daily_loss_pts"],
                        "instruments": instruments if isinstance(instruments, list) else [],
                        "params": raw_params,
                        "bias_config": bias_config,
                    })

            if instances:
                await self.spawn_for_tenant(tenant_id, nats_client, instances)
                logger.info("worker_reloaded", tenant_id=tenant_id, instances=len(instances))

        except Exception as exc:
            logger.error("worker_reload_failed", tenant_id=tenant_id, error=str(exc))

    async def stop_all(self):
        for tid, worker in self.workers.items():
            worker.stop()
        for tid, task in self._tasks.items():
            task.cancel()
        self.workers.clear()
        self._tasks.clear()


class _TenantWorker:
    """Background worker for one tenant — evaluates strategies on candle closes."""

    def __init__(self, tenant_id: str, candle_store, instances: list, nats_client):
        self.tenant_id = tenant_id
        self._candle_store = candle_store
        self._instances = instances
        self._nats = nats_client
        self._running = True
        self._tick_sub = None
        self._log = logger.bind(tenant_id=tenant_id)
        self._open_trades: list[dict] = []  # shared reference for status API

    def stop(self):
        self._running = False

    def get_instance_statuses(self) -> list[dict]:
        statuses = []
        for inst in self._instances:
            s = inst.to_status()
            for sym in inst.instruments:
                bars_5m = self._candle_store.get_bar_count(sym, "5m")
                warmed = self._candle_store.is_warmed_up(sym)
                stale = self._candle_store.is_tick_stale(sym)
                s.setdefault("candle_status", {})[sym] = {
                    "bars_5m": bars_5m, "warmed_up": warmed, "tick_stale": stale,
                }
            statuses.append(s)
        return statuses

    def get_open_trades(self) -> list[dict]:
        """Return currently open paper trades with live P&L."""
        return list(self._open_trades)

    async def run(self):
        import time
        from datetime import datetime, time as _time, timezone
        from services.user_worker_pool.pool.worker import (
            _normalize_symbol, _symbol_matches, _ChainSnapshot, _compute_options_overlay,
            NSE_OPEN_UTC, NSE_CLOSE_UTC, SESSION_RANGES,
        )
        from .db import rls_session
        from services.user_worker_pool.capital_tier import is_strategy_allowed, CapitalTier
        from services.user_worker_pool.strategies.base import Signal

        capital_tier = CapitalTier.STARTER
        latest_prices: dict[str, float] = {}
        last_eval_1m: dict[str, int] = {}
        last_signal: dict[str, tuple[str, float]] = {}  # key → (direction, timestamp)

        # Open paper trades — tracked for SL/TP/time-stop exits
        open_trades = self._open_trades

        # Subscribe to ticks
        async def on_tick(msg):
            try:
                data = json.loads(msg.data.decode())
                symbol = data.get("symbol", "")
                price = data.get("last_price", 0.0)
                volume = float(data.get("volume", 0))
                ts_raw = data.get("timestamp")
                if isinstance(ts_raw, str):
                    try:
                        from datetime import datetime as _dt
                        tick_ts = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        tick_ts = time.time()
                elif isinstance(ts_raw, (int, float)):
                    tick_ts = float(ts_raw)
                else:
                    tick_ts = time.time()

                if symbol and price > 0:
                    normalized = _normalize_symbol(symbol)
                    p = float(price)
                    # Store candles under all names (for warmup/tick matching)
                    self._candle_store.on_tick(normalized, p, volume, tick_ts)
                    if normalized != symbol:
                        self._candle_store.on_tick(symbol, p, volume, tick_ts)
                    from services.user_worker_pool.pool.worker import _SYMBOL_ALIASES
                    alias = _SYMBOL_ALIASES.get(normalized)
                    if alias:
                        self._candle_store.on_tick(alias, p, volume, tick_ts)
                    # Only evaluate under CANONICAL name (no duplicates)
                    latest_prices[normalized] = p
            except Exception:
                pass

        self._tick_sub = await self._nats.subscribe("ticks.>", cb=on_tick)

        # Start fallback poller
        poller_task = asyncio.create_task(self._candle_store.run_fallback_poller())

        self._log.info("worker_running", instances=len(self._instances))

        try:
            while self._running:
                await asyncio.sleep(1)

                now_ts = time.time()
                current_1m = int(now_ts // 60) * 60
                now_utc = datetime.now(timezone.utc)
                current_time = now_utc.time()

                # Market hours check
                if current_time < NSE_OPEN_UTC or current_time > NSE_CLOSE_UTC:
                    continue

                # ── Check open trades for SL/TP/time-stop exits ──────────
                still_open = []
                for ot in open_trades:
                    sym = ot["symbol"]
                    curr_price = latest_prices.get(sym, 0)
                    if curr_price <= 0:
                        still_open.append(ot)
                        continue

                    ot["bars_held"] += 1
                    d = ot["direction"]
                    exit_reason = None
                    exit_price = curr_price

                    # Check SL
                    if d == "BUY" and curr_price <= ot["sl"]:
                        exit_reason = "STOP LOSS"
                        exit_price = ot["sl"]
                    elif d == "SELL" and curr_price >= ot["sl"]:
                        exit_reason = "STOP LOSS"
                        exit_price = ot["sl"]
                    # Check TP
                    elif d == "BUY" and curr_price >= ot["tp"]:
                        exit_reason = "TARGET"
                        exit_price = ot["tp"]
                    elif d == "SELL" and curr_price <= ot["tp"]:
                        exit_reason = "TARGET"
                        exit_price = ot["tp"]
                    # Check time stop
                    elif ot["bars_held"] >= ot["max_hold_bars"]:
                        exit_reason = "TIME STOP"
                    # Check market close
                    elif current_time > NSE_CLOSE_UTC:
                        exit_reason = "SQUARE OFF"

                    if exit_reason:
                        pnl = (exit_price - ot["entry_price"]) if d == "BUY" else (ot["entry_price"] - exit_price)
                        ot["status"] = exit_reason
                        ot["exit_price"] = round(exit_price, 2)
                        ot["pnl"] = round(pnl, 2)

                        # Record to paper_trades DB
                        try:
                            async with rls_session(self.tenant_id) as db_session:
                                await db_session.execute(text("""
                                    INSERT INTO paper_trades
                                        (tenant_id, instance_id, strategy_name, instance_name,
                                         instrument, direction, entry_price, exit_price,
                                         stop_loss, target, entry_time, exit_time,
                                         exit_reason, pnl_points)
                                    VALUES
                                        (:tid, :iid, :sname, :iname,
                                         :instrument, :direction, :entry, :exit_price,
                                         :sl, :tp, to_timestamp(:entry_ts), NOW(),
                                         :reason, :pnl)
                                """), {
                                    "tid": self.tenant_id,
                                    "iid": ot.get("instance_id", "00000000-0000-0000-0000-000000000000"),
                                    "sname": ot.get("strategy_name", ""),
                                    "iname": ot["instance_name"],
                                    "instrument": sym,
                                    "direction": d,
                                    "entry": ot["entry_price"],
                                    "exit_price": exit_price,
                                    "sl": ot["sl"],
                                    "tp": ot["tp"],
                                    "entry_ts": ot["entry_time"],
                                    "reason": exit_reason.lower().replace(" ", "_"),
                                    "pnl": pnl,
                                })
                        except Exception:
                            pass

                        self._log.info("trade_closed",
                            instance=ot["instance_name"], symbol=sym,
                            direction=d, reason=exit_reason,
                            entry=ot["entry_price"], exit=exit_price, pnl=round(pnl, 2))
                    else:
                        ot["current_price"] = curr_price
                        if d == "BUY":
                            ot["unrealized_pnl"] = round(curr_price - ot["entry_price"], 2)
                        else:
                            ot["unrealized_pnl"] = round(ot["entry_price"] - curr_price, 2)
                        still_open.append(ot)
                open_trades = still_open

                if not latest_prices and not hasattr(self, '_no_tick_logged'):
                    self._log.warning("no_ticks_yet", msg="latest_prices is empty — no ticks received from NATS")
                    self._no_tick_logged = True

                for symbol, price in list(latest_prices.items()):
                    last_eval = last_eval_1m.get(symbol, 0)
                    if current_1m <= last_eval:
                        continue
                    last_eval_1m[symbol] = current_1m

                    candles_5m = self._candle_store.get_candles(symbol, "5m")
                    candles_1m = self._candle_store.get_candles(symbol, "1m")
                    n5 = len(candles_5m.get("close", [])) if candles_5m else 0

                    if n5 < 15:
                        if not hasattr(self, '_low_candle_logged'):
                            self._log.warning("low_candle_data", symbol=symbol, bars_5m=n5, msg="need 15+ 5m bars")
                            self._low_candle_logged = True
                        continue

                    # Log first successful evaluation
                    if not hasattr(self, '_first_eval_logged'):
                        self._log.info("first_evaluation", symbol=symbol, price=price, bars_5m=n5)
                        self._first_eval_logged = True

                    chain_data = {
                        "underlying": symbol,
                        "underlying_price": price,
                        "atm_iv": 0.15,
                        "segment": "nse",
                        "strikes": [],
                        "candles_1m": candles_1m,
                        "candles_5m": candles_5m,
                    }
                    chain = _ChainSnapshot(chain_data)

                    for inst in self._instances:
                        if not _symbol_matches(symbol, inst.instruments):
                            continue
                        if not inst.is_in_session(current_time):
                            continue
                        if inst.check_daily_limit():
                            continue

                        inst.last_evaluated_ts = time.time()
                        inst.total_evaluations += 1

                        # Bias gate
                        if inst.bias_evaluator:
                            try:
                                bias_dir = inst.bias_evaluator.get_current_bias(candles_5m, candles_1m)
                                inst.last_bias = bias_dir or ""
                                if bias_dir is None:
                                    continue
                            except Exception:
                                continue
                        else:
                            bias_dir = None

                        try:
                            signal = inst.strategy.evaluate(chain, None, [], inst.config)
                        except Exception:
                            continue

                        if signal is None:
                            continue

                        sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
                        self._log.debug("strategy_returned_signal", instance=inst.instance_name, symbol=symbol, direction=sig_dir)
                        if bias_dir and sig_dir != bias_dir:
                            continue

                        # Track signal
                        if sig_dir == "BUY":
                            inst.signals_today_buy += 1
                        else:
                            inst.signals_today_sell += 1
                        inst.last_signal_ts = time.time()

                        # Tag signal
                        signal.metadata["instance_id"] = inst.instance_id
                        signal.metadata["instance_name"] = inst.instance_name
                        signal.metadata["trading_mode"] = inst.mode
                        if bias_dir:
                            signal.metadata["bias_direction"] = bias_dir

                        # Dedup: skip if same direction fired within last 30 minutes
                        dedup_key = f"{inst.instance_id}:{symbol}"
                        prev = last_signal.get(dedup_key)
                        if prev and prev[0] == sig_dir and (time.time() - prev[1]) < 1800:
                            continue
                        last_signal[dedup_key] = (sig_dir, time.time())

                        self._log.info("signal_pre_process", instance=inst.instance_name, symbol=symbol, direction=sig_dir)

                        # Compute ATR-based INDEX PRICE entry/SL/TP (not option premiums)
                        from services.user_worker_pool.bias.evaluator import atr_full as _atr_full
                        atr_val = 0.0
                        if n5 >= 15:
                            c5 = candles_5m["close"]
                            h5 = candles_5m["high"]
                            l5 = candles_5m["low"]
                            atr_arr = _atr_full(h5, l5, c5, 14)
                            if len(atr_arr) > 0:
                                atr_val = float(atr_arr[-1])

                        # Get exit config from instance params
                        sl_atr_mult = float(inst.config.get("sl_atr_mult", 0.5))
                        tp_atr_mult = float(inst.config.get("tp_atr_mult", 1.5))
                        max_sl = float(inst.config.get("max_sl_points", 50))
                        slippage = float(inst.config.get("slippage_pts", 0.5))

                        # Index price based entry/SL/TP
                        entry_price = price + (slippage if sig_dir == "BUY" else -slippage)
                        sl_dist = min(sl_atr_mult * atr_val, max_sl) if atr_val > 0 else max_sl
                        tp_dist = tp_atr_mult * atr_val if atr_val > 0 else max_sl * 2

                        if sig_dir == "BUY":
                            sl_price = round(entry_price - sl_dist, 2)
                            tp_price = round(entry_price + tp_dist, 2)
                        else:
                            sl_price = round(entry_price + sl_dist, 2)
                            tp_price = round(entry_price - tp_dist, 2)

                        # Override strategy's option prices with index prices
                        signal.entry_price = round(entry_price, 2)
                        signal.stop_loss_price = sl_price
                        signal.target_price = tp_price

                        # Options overlay (suggestion only)
                        opts = _compute_options_overlay(chain, signal)
                        if opts:
                            signal.metadata["options"] = opts

                        idx_risk = round(sl_dist, 2)
                        idx_reward = round(tp_dist, 2)

                        # Publish signal with INDEX prices
                        sig_payload = {
                            "signal_type": "DIRECT",
                            "signal": signal.strategy_name,
                            "underlying": symbol,
                            "direction": sig_dir,
                            "entry_price": signal.entry_price,
                            "stop_loss_price": signal.stop_loss_price,
                            "target_price": signal.target_price,
                            "index_risk_pts": idx_risk,
                            "index_reward_pts": idx_reward,
                            "index_rr": f"1:{round(idx_reward / idx_risk, 1)}" if idx_risk > 0 else "N/A",
                            "atr": round(atr_val, 2),
                            "confidence": signal.confidence,
                            "has_options_chain": bool(opts),
                            "metadata": signal.metadata,
                        }
                        if opts:
                            sig_payload["options"] = opts

                        await self._nats.publish(
                            f"signals.{self.tenant_id}.{signal.strategy_name}.{symbol}",
                            json.dumps(sig_payload).encode(),
                        )

                        # Persist signal to DB so /signals page can show it
                        try:
                            async with rls_session(self.tenant_id) as db_session:
                                await db_session.execute(text("""
                                    INSERT INTO signals
                                        (tenant_id, time, strategy, underlying, segment, direction,
                                         strength, regime, legs, stop_loss_pct, rationale, acted_upon)
                                    VALUES
                                        (:tid, NOW(), :strategy, :underlying, 'NSE_INDEX', :direction,
                                         :strength, 'UNKNOWN', :legs, :sl_pct, :rationale, FALSE)
                                """), {
                                    "tid": self.tenant_id,
                                    "strategy": inst.instance_name,
                                    "underlying": symbol,
                                    "direction": sig_dir,
                                    "strength": signal.confidence,
                                    "legs": json.dumps(sig_payload),
                                    "sl_pct": 0.0,
                                    "rationale": json.dumps({
                                        "instance": inst.instance_name,
                                        "mode": inst.mode,
                                        "bias": bias_dir,
                                        "entry_price": float(signal.entry_price) if signal.entry_price else 0,
                                        "stop_loss_price": float(signal.stop_loss_price) if signal.stop_loss_price else 0,
                                        "target_price": float(signal.target_price) if signal.target_price else 0,
                                        "options": sig_payload.get("options"),
                                    }),
                                })
                        except Exception as db_err:
                            self._log.warning("signal_db_write_failed", error=str(db_err))

                        # Track as open paper trade for live SL/TP monitoring
                        max_hold_bars = int(inst.config.get("max_hold_bars", 20))
                        open_trades.append({
                            "symbol": symbol,
                            "direction": sig_dir,
                            "entry_price": signal.entry_price,
                            "sl": signal.stop_loss_price,
                            "tp": signal.target_price,
                            "entry_time": time.time(),
                            "max_hold_bars": max_hold_bars,
                            "bars_held": 0,
                            "instance_name": inst.instance_name,
                            "status": "OPEN",
                        })

                        self._log.info(
                            "signal_generated",
                            instance=inst.instance_name,
                            strategy=inst.strategy.name,
                            symbol=symbol,
                            direction=sig_dir,
                            entry=signal.entry_price,
                            sl=signal.stop_loss_price,
                            tp=signal.target_price,
                            mode=inst.mode,
                        )

        finally:
            poller_task.cancel()
            if self._tick_sub:
                try:
                    await self._tick_sub.unsubscribe()
                except Exception:
                    pass


async def start_worker_pool(app, nats_client) -> SimpleWorkerPool:
    """Load all tenants with enabled instances and spawn workers."""
    pool = SimpleWorkerPool()

    try:
        # Query all tenants with enabled instances — use raw session to bypass RLS
        from .db import async_session_factory

        async with async_session_factory() as session:
            result = await session.execute(text("""
                SELECT DISTINCT tenant_id FROM user_strategy_configs
                WHERE enabled = TRUE AND trading_mode IN ('live', 'paper')
            """))
            tenant_ids = [str(row[0]) for row in result.fetchall()]

        logger.info("worker_pool_loading", tenants=len(tenant_ids))

        for tenant_id in tenant_ids:
            # Load instances for this tenant
            try:
                async with rls_session(tenant_id) as session:
                    result = await session.execute(text("""
                        SELECT id, strategy_name, instance_name, params, enabled,
                               trading_mode, session, max_daily_loss_pts
                        FROM user_strategy_configs
                        WHERE tenant_id = :tid AND enabled = TRUE
                              AND trading_mode IN ('live', 'paper')
                        ORDER BY strategy_name
                    """), {"tid": tenant_id})

                    instances = []
                    for row in result.mappings().all():
                        raw_params = row["params"] if isinstance(row["params"], dict) else {}
                        instruments = raw_params.pop("instruments", [])
                        bias_config = raw_params.pop("bias_config", None)

                        instances.append({
                            "instance_id": str(row["id"]),
                            "instance_name": row["instance_name"] or row["strategy_name"],
                            "strategy_name": row["strategy_name"],
                            "mode": row["trading_mode"] or "paper",
                            "session": row["session"] or "all",
                            "max_daily_loss_pts": row["max_daily_loss_pts"],
                            "instruments": instruments if isinstance(instruments, list) else [],
                            "params": raw_params,
                            "bias_config": bias_config,
                        })

                if instances:
                    await pool.spawn_for_tenant(tenant_id, nats_client, instances)

            except Exception as exc:
                logger.error("tenant_load_failed", tenant_id=tenant_id, error=str(exc))

    except Exception as exc:
        logger.error("worker_pool_load_failed", error=str(exc))

    # Subscribe to config reload events — respawn worker when user changes instances
    async def _on_config_reload(msg):
        try:
            data = json.loads(msg.data)
            tid = data.get("tenant_id")
            if tid:
                logger.info("worker_config_reload", tenant_id=tid, event=data.get("event"))
                await pool.reload_tenant(tid, nats_client)
        except Exception as exc:
            logger.error("config_reload_error", error=str(exc))

    await nats_client.subscribe("worker.config_reload.>", cb=_on_config_reload)
    logger.info("worker_pool_started", workers=len(pool.workers))
    return pool
