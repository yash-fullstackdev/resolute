/// PyO3 entry point — exposes `run_backtest` to Python.

mod data;
mod engine;
mod indicators;
mod types;

use std::collections::HashMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::types::{EngineConfig, StrategyEngineConfig};

/// Run a full backtest.
///
/// Args (from Python):
///   data_dir: str — path to data/ directory
///   instruments: list[str] — e.g. ["NIFTY_50"]
///   start_ts: float — Unix epoch start (inclusive)
///   end_ts: float — Unix epoch end (inclusive)
///   strategy_callbacks: list[Any] — Python objects with .evaluate_bar() method
///   strategy_configs: list[dict] — per-strategy config dicts
///   engine_cfg: dict — global engine config
///
/// Returns: dict with full backtest results
#[pyfunction]
fn run_backtest(
    py: Python<'_>,
    data_dir: String,
    instruments: Vec<String>,
    start_ts: f64,
    end_ts: f64,
    strategy_callbacks: Vec<PyObject>,
    strategy_configs: Vec<&PyDict>,
    engine_cfg: &PyDict,
) -> PyResult<PyObject> {
    use std::path::Path;

    // Parse engine config
    let ecfg = EngineConfig {
        initial_capital: get_f64(engine_cfg, "initial_capital", 500_000.0),
        slippage_mode: get_str(engine_cfg, "slippage_mode", "fixed"),
        slippage_value: get_f64(engine_cfg, "slippage_value", 0.5),
        brokerage_per_trade: get_f64(engine_cfg, "brokerage_per_trade", 20.0),
        brokerage_pct: get_f64(engine_cfg, "brokerage_pct", 0.0),
        ambiguous_bar_resolution: get_str(engine_cfg, "ambiguous_bar_resolution", "worst_case"),
    };

    // Parse per-strategy configs
    let scfgs: Vec<StrategyEngineConfig> = strategy_configs.iter().map(|d| {
        StrategyEngineConfig {
            name: get_str(d, "name", "unknown"),
            primary_tf_minutes: get_u32(d, "primary_tf_minutes", 5),
            active_start_minutes: get_u32(d, "active_start_minutes", 9 * 60 + 20),
            active_end_minutes: get_u32(d, "active_end_minutes", 14 * 60 + 30),
            square_off_minutes: get_u32(d, "square_off_minutes", 15 * 60 + 15),
            capital_allocation: get_f64(d, "capital_allocation", 100_000.0),
            max_positions: get_u32(d, "max_positions", 3),
            max_drawdown_pct: get_f64(d, "max_drawdown_pct", 20.0),
            max_loss_per_day: get_f64(d, "max_loss_per_day", 0.0),
            lot_size: get_u32(d, "lot_size", 75),
        }
    }).collect();

    let data_path = Path::new(&data_dir);

    // Collect all required TFs
    let mut required_tfs: std::collections::HashSet<u32> = std::collections::HashSet::new();
    for cfg in &scfgs {
        required_tfs.insert(cfg.primary_tf_minutes);
        required_tfs.insert(1); // always need 1m
    }

    // Load data for each instrument and run backtest per-instrument (combine at reporting layer)
    // For multi-instrument, we run per-instrument and strategies filter by chain.underlying
    // Here we combine all instruments into one array (strategies handle filtering via adapter)
    let mut combined_1m = types::CandleArray::default();
    for instrument in &instruments {
        let arr = data::load_instrument(data_path, instrument, start_ts, end_ts);
        combined_1m.open.extend_from_slice(&arr.open);
        combined_1m.high.extend_from_slice(&arr.high);
        combined_1m.low.extend_from_slice(&arr.low);
        combined_1m.close.extend_from_slice(&arr.close);
        combined_1m.volume.extend_from_slice(&arr.volume);
        combined_1m.timestamp.extend_from_slice(&arr.timestamp);
    }

    // Sort by timestamp (important when merging multiple instruments)
    let mut indices: Vec<usize> = (0..combined_1m.len()).collect();
    indices.sort_by(|&a, &b| combined_1m.timestamp[a].partial_cmp(&combined_1m.timestamp[b]).unwrap());
    let sort_arr = |v: &Vec<f64>| indices.iter().map(|&i| v[i]).collect::<Vec<f64>>();
    combined_1m.open = sort_arr(&combined_1m.open);
    combined_1m.high = sort_arr(&combined_1m.high);
    combined_1m.low = sort_arr(&combined_1m.low);
    combined_1m.close = sort_arr(&combined_1m.close);
    combined_1m.volume = sort_arr(&combined_1m.volume);
    combined_1m.timestamp = sort_arr(&combined_1m.timestamp);

    // Pre-aggregate for each required TF
    let mut candles_tf: HashMap<u32, types::CandleArray> = HashMap::new();
    for tf in &required_tfs {
        if *tf == 1 {
            candles_tf.insert(1, combined_1m.clone());
        } else {
            candles_tf.insert(*tf, data::aggregate(&combined_1m, *tf));
        }
    }

    // Run the engine
    let raw = engine::run(
        py,
        &combined_1m,
        &candles_tf,
        &strategy_callbacks,
        &scfgs,
        &ecfg,
    )?;

    // Convert RawBacktestResult to Python dict
    let result = PyDict::new(py);

    // Trades
    let trades_list = PyList::empty(py);
    for t in &raw.trades {
        let td = PyDict::new(py);
        td.set_item("strategy_name", &t.strategy_name)?;
        td.set_item("direction", t.direction)?;
        td.set_item("entry_price", t.entry_price)?;
        td.set_item("exit_price", t.exit_price)?;
        td.set_item("stop_loss", t.stop_loss)?;
        td.set_item("target", t.target)?;
        td.set_item("hold_candles", t.hold_candles)?;
        td.set_item("exit_bar_ts", t.exit_bar_ts)?;
        td.set_item("quantity", t.quantity)?;
        td.set_item("lot_size", t.lot_size)?;
        td.set_item("pnl", t.pnl)?;
        td.set_item("slippage", t.slippage)?;
        td.set_item("brokerage", t.brokerage)?;
        td.set_item("exit_reason", &t.exit_reason)?;
        td.set_item("tag", &t.tag)?;
        trades_list.append(td)?;
    }
    result.set_item("trades", trades_list)?;

    // Combined equity curve
    let eq_list = PyList::empty(py);
    for snap in &raw.equity_snapshots {
        let ed = PyDict::new(py);
        ed.set_item("timestamp", snap.timestamp)?;
        ed.set_item("equity", snap.equity)?;
        ed.set_item("drawdown_pct", snap.drawdown_pct)?;
        eq_list.append(ed)?;
    }
    result.set_item("equity_curve", eq_list)?;

    // Per-strategy equity
    let per_s_eq = PyDict::new(py);
    for (s_idx, name) in raw.strategy_names.iter().enumerate() {
        let s_eq_list = PyList::empty(py);
        for snap in &raw.per_strategy_equity[s_idx] {
            let ed = PyDict::new(py);
            ed.set_item("timestamp", snap.timestamp)?;
            ed.set_item("equity", snap.equity)?;
            ed.set_item("drawdown_pct", snap.drawdown_pct)?;
            s_eq_list.append(ed)?;
        }
        per_s_eq.set_item(name, s_eq_list)?;
    }
    result.set_item("per_strategy_equity", per_s_eq)?;

    // Per-strategy trades
    let per_s_trades = PyDict::new(py);
    for (s_idx, name) in raw.strategy_names.iter().enumerate() {
        let s_trades_list = PyList::empty(py);
        for t in &raw.per_strategy_trades[s_idx] {
            let td = PyDict::new(py);
            td.set_item("strategy_name", &t.strategy_name)?;
            td.set_item("direction", t.direction)?;
            td.set_item("entry_price", t.entry_price)?;
            td.set_item("exit_price", t.exit_price)?;
            td.set_item("pnl", t.pnl)?;
            td.set_item("exit_reason", &t.exit_reason)?;
            s_trades_list.append(td)?;
        }
        per_s_trades.set_item(name, s_trades_list)?;
    }
    result.set_item("per_strategy_trades", per_s_trades)?;

    result.set_item("strategy_names", &raw.strategy_names)?;
    result.set_item("start_ts", raw.start_ts)?;
    result.set_item("end_ts", raw.end_ts)?;
    result.set_item("initial_capital", raw.initial_capital)?;

    Ok(result.into())
}

// ── Helper extractors ─────────────────────────────────────────────────────────

fn get_f64(d: &PyDict, key: &str, default: f64) -> f64 {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<f64>().ok())
        .unwrap_or(default)
}

fn get_u32(d: &PyDict, key: &str, default: u32) -> u32 {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<u32>().ok())
        .unwrap_or(default)
}

fn get_str(d: &PyDict, key: &str, default: &str) -> String {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<String>().ok())
        .unwrap_or_else(|| default.to_string())
}

// ── Module registration ───────────────────────────────────────────────────────

#[pymodule]
fn backtest_engine(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_backtest, m)?)?;
    Ok(())
}
