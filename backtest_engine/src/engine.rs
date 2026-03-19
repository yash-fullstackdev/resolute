/// Core backtest event loop, order simulator, and portfolio engine.

use std::collections::HashMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use numpy::PyArray1;

use crate::data::{build_tf_close_set, ts_to_ist_minutes};
use crate::types::{
    CandleArray, EngineConfig, EquitySnapshot, Position, RawBacktestResult,
    RawSignal, StrategyEngineConfig, TradeRecord,
};

// ── Order Simulator ───────────────────────────────────────────────────────────

struct PendingOrder {
    strategy_idx: usize,
    signal: RawSignal,
    submitted_bar: usize,
    ttl_bars: u32,
}

struct OrderSimulator {
    slippage_mode: String,
    slippage_value: f64,
    brokerage_per_trade: f64,
    ambiguous_resolution: String,
}

impl OrderSimulator {
    fn try_fill_market(&self, signal: &RawSignal, bar_open: f64) -> (f64, f64, f64) {
        let slip = self.calc_slippage(bar_open, signal.direction);
        let fill_price = bar_open + slip * signal.direction as f64;
        let brok = self.calc_brokerage(fill_price, signal.quantity);
        (fill_price, slip.abs(), brok)
    }

    fn calc_slippage(&self, price: f64, _dir: i8) -> f64 {
        match self.slippage_mode.as_str() {
            "percentage" => price * self.slippage_value / 100.0,
            _ => self.slippage_value, // "fixed"
        }
    }

    fn calc_brokerage(&self, _price: f64, _qty: i32) -> f64 {
        self.brokerage_per_trade
    }

    /// Check if stop/target hit within bar. Returns exit_price and reason.
    fn check_exit_within_bar(
        &self,
        pos: &Position,
        bar_high: f64,
        bar_low: f64,
    ) -> Option<(f64, String)> {
        let sl_hit = match pos.direction {
            1 => bar_low <= pos.stop_loss,    // long: low hits SL
            -1 => bar_high >= pos.stop_loss,  // short: high hits SL
            _ => false,
        };
        let tgt_hit = match pos.direction {
            1 => bar_high >= pos.target,
            -1 => bar_low <= pos.target,
            _ => false,
        };

        match (sl_hit, tgt_hit) {
            (true, true) => {
                // Ambiguous: both hit in same bar
                match self.ambiguous_resolution.as_str() {
                    "optimistic" => Some((pos.target, "target".to_string())),
                    _ => Some((pos.stop_loss, "stop_loss".to_string())), // worst_case default
                }
            }
            (true, false) => Some((pos.stop_loss, "stop_loss".to_string())),
            (false, true) => Some((pos.target, "target".to_string())),
            (false, false) => None,
        }
    }
}

// ── Portfolio Engine ──────────────────────────────────────────────────────────

struct StrategyPortfolio {
    capital: f64,
    peak_capital: f64,
    daily_pnl: f64,
    open_positions: Vec<Position>,
    killed: bool,
    daily_trade_count: u32,
}

impl StrategyPortfolio {
    fn new(capital: f64) -> Self {
        StrategyPortfolio {
            capital,
            peak_capital: capital,
            daily_pnl: 0.0,
            open_positions: Vec::new(),
            killed: false,
            daily_trade_count: 0,
        }
    }

    fn drawdown_pct(&self) -> f64 {
        if self.peak_capital <= 0.0 {
            return 0.0;
        }
        (self.peak_capital - self.capital) / self.peak_capital * 100.0
    }

    fn apply_close(&mut self, pnl: f64) {
        self.capital += pnl;
        if self.capital > self.peak_capital {
            self.peak_capital = self.capital;
        }
        self.daily_pnl += pnl;
        self.daily_trade_count += 1;
    }
}

// ── Main Event Loop ───────────────────────────────────────────────────────────

pub fn run(
    py: Python<'_>,
    candles_1m: &CandleArray,
    candles_tf: &HashMap<u32, CandleArray>, // tf_minutes -> aggregated candles
    strategy_callbacks: &[PyObject],
    strategy_configs: &[StrategyEngineConfig],
    engine_config: &EngineConfig,
) -> PyResult<RawBacktestResult> {
    let n_strategies = strategy_configs.len();
    let n_bars = candles_1m.len();

    if n_bars == 0 || n_strategies == 0 {
        return Ok(RawBacktestResult {
            trades: vec![],
            equity_snapshots: vec![],
            per_strategy_equity: vec![vec![]; n_strategies],
            per_strategy_trades: vec![vec![]; n_strategies],
            strategy_names: strategy_configs.iter().map(|c| c.name.clone()).collect(),
            start_ts: 0.0,
            end_ts: 0.0,
            initial_capital: engine_config.initial_capital,
        });
    }

    // Pre-build TF close sets for each unique timeframe
    let mut tf_close_sets: HashMap<u32, Vec<bool>> = HashMap::new();
    for cfg in strategy_configs {
        tf_close_sets
            .entry(cfg.primary_tf_minutes)
            .or_insert_with(|| build_tf_close_set(candles_1m, cfg.primary_tf_minutes));
    }

    // Pre-build index maps (1m bar -> TF bar index) for each TF
    let mut tf_index_maps: HashMap<u32, Vec<usize>> = HashMap::new();
    for tf in candles_tf.keys() {
        tf_index_maps.insert(*tf, crate::data::build_1m_to_tf_index(candles_1m, *tf));
    }

    let order_sim = OrderSimulator {
        slippage_mode: engine_config.slippage_mode.clone(),
        slippage_value: engine_config.slippage_value,
        brokerage_per_trade: engine_config.brokerage_per_trade,
        ambiguous_resolution: engine_config.ambiguous_bar_resolution.clone(),
    };

    let mut portfolios: Vec<StrategyPortfolio> = strategy_configs
        .iter()
        .map(|c| StrategyPortfolio::new(c.capital_allocation))
        .collect();

    let mut all_trades: Vec<TradeRecord> = Vec::new();
    let mut per_strategy_trades: Vec<Vec<TradeRecord>> = vec![vec![]; n_strategies];

    // Combined equity = sum of per-strategy capitals
    let total_capital = strategy_configs.iter().map(|c| c.capital_allocation).sum::<f64>();
    let mut equity_snapshots: Vec<EquitySnapshot> = Vec::with_capacity(n_bars);
    let mut per_strategy_equity: Vec<Vec<EquitySnapshot>> = vec![Vec::with_capacity(n_bars); n_strategies];
    let mut peak_total = total_capital;

    let mut position_id_counter: u64 = 0;
    let mut prev_day: i64 = -1;

    // Export full 1m candle arrays to Python once (they're passed as references per bar)
    let py_candles_1m = candles_to_py_dict(py, candles_1m)?;

    // Export per-TF candle arrays to Python
    let mut py_candles_tf: HashMap<u32, Py<PyDict>> = HashMap::new();
    for (tf, arr) in candles_tf {
        py_candles_tf.insert(*tf, candles_to_py_dict(py, arr)?.into());
    }

    // ── Main bar loop ──────────────────────────────────────────────────────
    for bar_idx in 0..n_bars {
        let ts = candles_1m.timestamp[bar_idx];
        let ist_min = ts_to_ist_minutes(ts);

        // Day boundary reset
        let day = (ts as i64 + 330 * 60) / 86400;
        if day != prev_day {
            for p in &mut portfolios {
                p.daily_pnl = 0.0;
                p.daily_trade_count = 0;
            }
            prev_day = day;
        }

        // Bar OHLC
        let bar_high = candles_1m.high[bar_idx];
        let bar_low = candles_1m.low[bar_idx];

        // ── Per-strategy processing ────────────────────────────────────────
        for s_idx in 0..n_strategies {
            let cfg = &strategy_configs[s_idx];
            let port = &mut portfolios[s_idx];

            if port.killed {
                continue;
            }

            // Check exit conditions on open positions (stop/target/time-stop)
            let mut closed_ids: Vec<u64> = Vec::new();
            {
                let positions_clone = port.open_positions.clone();
                for pos in &positions_clone {
                    // Time-stop
                    if bar_idx >= pos.time_stop_bar {
                        let exit_price = candles_1m.open[bar_idx.min(n_bars - 1)];
                        let slip = order_sim.calc_slippage(exit_price, -pos.direction);
                        let fill = exit_price - slip * pos.direction as f64;
                        let pnl = (fill - pos.entry_price) * pos.direction as f64
                            * pos.quantity as f64
                            * pos.lot_size as f64
                            - pos.entry_cost
                            - order_sim.calc_brokerage(fill, pos.quantity);
                        port.apply_close(pnl);
                        let record = make_trade(pos, bar_idx, ts, fill, pnl, slip, order_sim.brokerage_per_trade, "time_stop");
                        per_strategy_trades[s_idx].push(record.clone());
                        all_trades.push(record);
                        closed_ids.push(pos.id);
                        continue;
                    }

                    // Stop / Target
                    if let Some((exit_price, reason)) =
                        order_sim.check_exit_within_bar(pos, bar_high, bar_low)
                    {
                        let slip = order_sim.calc_slippage(exit_price, -pos.direction);
                        let fill = exit_price - slip * pos.direction as f64;
                        let pnl = (fill - pos.entry_price) * pos.direction as f64
                            * pos.quantity as f64
                            * pos.lot_size as f64
                            - pos.entry_cost
                            - order_sim.calc_brokerage(fill, pos.quantity);
                        port.apply_close(pnl);
                        let record = make_trade(pos, bar_idx, ts, fill, pnl, slip, order_sim.brokerage_per_trade, &reason);
                        per_strategy_trades[s_idx].push(record.clone());
                        all_trades.push(record);
                        closed_ids.push(pos.id);
                    }
                }
            }
            port.open_positions.retain(|p| !closed_ids.contains(&p.id));

            // Check if strategy's primary TF bar just closed
            let tf_closes = tf_close_sets.get(&cfg.primary_tf_minutes);
            let bar_closes_tf = tf_closes.map(|v| v.get(bar_idx).copied().unwrap_or(false)).unwrap_or(false);

            if !bar_closes_tf {
                continue;
            }

            // Session window check
            if ist_min < cfg.active_start_minutes || ist_min > cfg.active_end_minutes {
                continue;
            }

            // Square-off time: force close all positions
            if ist_min >= cfg.square_off_minutes {
                let positions_clone = port.open_positions.clone();
                for pos in &positions_clone {
                    let exit_price = candles_1m.close[bar_idx];
                    let pnl = (exit_price - pos.entry_price) * pos.direction as f64
                        * pos.quantity as f64
                        * pos.lot_size as f64
                        - pos.entry_cost
                        - order_sim.calc_brokerage(exit_price, pos.quantity);
                    port.apply_close(pnl);
                    let record = make_trade(pos, bar_idx, ts, exit_price, pnl, 0.0, order_sim.brokerage_per_trade, "square_off");
                    per_strategy_trades[s_idx].push(record.clone());
                    all_trades.push(record);
                }
                port.open_positions.clear();
                continue;
            }

            // Max positions cap
            if port.open_positions.len() >= cfg.max_positions as usize {
                continue;
            }

            // Drawdown kill — disabled for index-point backtesting
            // (goal is to capture all signals, not simulate capital management)

            // Daily loss limit
            if cfg.max_loss_per_day > 0.0 && port.daily_pnl <= -cfg.max_loss_per_day {
                continue;
            }

            // Call Python strategy
            let tf = cfg.primary_tf_minutes;
            let tf_idx = tf_index_maps
                .get(&tf)
                .and_then(|m| m.get(bar_idx).copied())
                .unwrap_or(0);

            let py_tf_candles: &PyDict = match py_candles_tf.get(&tf) {
                Some(d) => d.as_ref(py),
                None => py_candles_1m,
            };

            let open_positions_py = positions_to_py_list(py, &port.open_positions)?;

            let result = strategy_callbacks[s_idx].call_method(
                py,
                "evaluate_bar",
                (bar_idx, tf_idx, py_candles_1m, py_tf_candles, open_positions_py),
                None,
            );

            match result {
                Ok(signal_obj) => {
                    if signal_obj.is_none(py) {
                        continue;
                    }
                    if let Some(signal) = py_to_signal(py, &signal_obj)? {
                        // Fill on next bar open (or current bar close for simplicity)
                        let fill_bar = (bar_idx + 1).min(n_bars - 1);
                        let fill_bar_open = candles_1m.open[fill_bar];
                        // Use signal.entry_price directly when provided (e.g. option premium).
                        // Fall back to next-bar open + slippage for equity/futures strategies.
                        let (fill_price, slip, brok) = if signal.entry_price > 0.0 {
                            let brok = order_sim.calc_brokerage(signal.entry_price, signal.quantity);
                            (signal.entry_price, 0.0_f64, brok)
                        } else {
                            order_sim.try_fill_market(&signal, fill_bar_open)
                        };

                        let time_stop_bar = bar_idx + signal.time_stop_bars as usize;

                        position_id_counter += 1;
                        let pos = Position {
                            id: position_id_counter,
                            strategy_idx: s_idx,
                            direction: signal.direction,
                            entry_bar_idx: fill_bar,
                            entry_price: fill_price,
                            quantity: signal.quantity,
                            stop_loss: signal.stop_loss,
                            target: signal.target,
                            time_stop_bar,
                            strategy_name: cfg.name.clone(),
                            tag: signal.tag.clone(),
                            lot_size: cfg.lot_size,
                            entry_cost: slip + brok,
                        };
                        port.open_positions.push(pos);
                    }
                }
                Err(e) => {
                    // Log but don't crash the backtest
                    e.print_and_set_sys_last_vars(py);
                }
            }
        }

        // ── Equity snapshot ────────────────────────────────────────────────
        let total_equity: f64 = portfolios.iter().map(|p| p.capital).sum();
        if total_equity > peak_total {
            peak_total = total_equity;
        }
        let dd_pct = if peak_total > 0.0 {
            (peak_total - total_equity) / peak_total * 100.0
        } else {
            0.0
        };
        equity_snapshots.push(EquitySnapshot { timestamp: ts, equity: total_equity, drawdown_pct: dd_pct });

        for s_idx in 0..n_strategies {
            let p = &portfolios[s_idx];
            let peak = p.peak_capital;
            let dd = if peak > 0.0 { (peak - p.capital) / peak * 100.0 } else { 0.0 };
            per_strategy_equity[s_idx].push(EquitySnapshot {
                timestamp: ts,
                equity: p.capital,
                drawdown_pct: dd,
            });
        }
    }

    // Close any remaining open positions at last bar close
    let last_ts = candles_1m.timestamp[n_bars - 1];
    let last_close = candles_1m.close[n_bars - 1];
    for s_idx in 0..n_strategies {
        let port = &mut portfolios[s_idx];
        let positions_clone = port.open_positions.clone();
        for pos in &positions_clone {
            let pnl = (last_close - pos.entry_price) * pos.direction as f64
                * pos.quantity as f64
                * pos.lot_size as f64
                - pos.entry_cost
                - order_sim.calc_brokerage(last_close, pos.quantity);
            port.apply_close(pnl);
            let record = make_trade(&pos, n_bars - 1, last_ts, last_close, pnl, 0.0, order_sim.brokerage_per_trade, "end_of_backtest");
            per_strategy_trades[s_idx].push(record.clone());
            all_trades.push(record);
        }
        port.open_positions.clear();
    }

    Ok(RawBacktestResult {
        trades: all_trades,
        equity_snapshots,
        per_strategy_equity,
        per_strategy_trades,
        strategy_names: strategy_configs.iter().map(|c| c.name.clone()).collect(),
        start_ts: candles_1m.timestamp[0],
        end_ts: last_ts,
        initial_capital: engine_config.initial_capital,
    })
}

// ── Helper functions ──────────────────────────────────────────────────────────

fn candles_to_py_dict<'py>(py: Python<'py>, arr: &CandleArray) -> PyResult<&'py PyDict> {
    let d = PyDict::new(py);
    d.set_item("open", PyArray1::from_vec(py, arr.open.clone()))?;
    d.set_item("high", PyArray1::from_vec(py, arr.high.clone()))?;
    d.set_item("low", PyArray1::from_vec(py, arr.low.clone()))?;
    d.set_item("close", PyArray1::from_vec(py, arr.close.clone()))?;
    d.set_item("volume", PyArray1::from_vec(py, arr.volume.clone()))?;
    d.set_item("timestamp", PyArray1::from_vec(py, arr.timestamp.clone()))?;
    Ok(d)
}

fn positions_to_py_list<'py>(py: Python<'py>, positions: &[Position]) -> PyResult<&'py PyList> {
    let items: Vec<PyObject> = positions.iter().map(|p| {
        let d = PyDict::new(py);
        d.set_item("id", p.id).unwrap();
        d.set_item("direction", p.direction).unwrap();
        d.set_item("entry_price", p.entry_price).unwrap();
        d.set_item("quantity", p.quantity).unwrap();
        d.set_item("stop_loss", p.stop_loss).unwrap();
        d.set_item("target", p.target).unwrap();
        d.set_item("strategy_name", &p.strategy_name).unwrap();
        d.into()
    }).collect();
    Ok(PyList::new(py, items))
}

fn py_to_signal(py: Python<'_>, obj: &PyObject) -> PyResult<Option<RawSignal>> {
    if obj.is_none(py) {
        return Ok(None);
    }
    let d = obj.downcast::<PyDict>(py)?;
    let direction: i8 = d.get_item("direction")?
        .and_then(|v| v.extract::<i8>().ok())
        .unwrap_or(0);
    if direction == 0 {
        return Ok(None);
    }
    let entry_price = d.get_item("entry_price")?.and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let stop_loss = d.get_item("stop_loss")?.and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let target = d.get_item("target")?.and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let quantity = d.get_item("quantity")?.and_then(|v| v.extract::<i32>().ok()).unwrap_or(1);
    let time_stop_bars = d.get_item("time_stop_bars")?.and_then(|v| v.extract::<u32>().ok()).unwrap_or(75);
    let tag = d.get_item("tag")?.and_then(|v| v.extract::<String>().ok()).unwrap_or_default();

    if stop_loss <= 0.0 || target <= 0.0 {
        return Ok(None);
    }

    Ok(Some(RawSignal { direction, entry_price, stop_loss, target, quantity, time_stop_bars, tag }))
}

fn make_trade(pos: &Position, bar_idx: usize, exit_ts: f64, exit_price: f64, pnl: f64, slip: f64, brok: f64, reason: &str) -> TradeRecord {
    TradeRecord {
        strategy_name: pos.strategy_name.clone(),
        direction: pos.direction,
        entry_bar_ts: 0.0,
        exit_bar_ts: exit_ts,
        entry_price: pos.entry_price,
        exit_price,
        stop_loss: pos.stop_loss,
        target: pos.target,
        hold_candles: bar_idx.saturating_sub(pos.entry_bar_idx),
        quantity: pos.quantity,
        lot_size: pos.lot_size,
        pnl,
        slippage: slip,
        brokerage: brok,
        exit_reason: reason.to_string(),
        tag: pos.tag.clone(),
    }
}
