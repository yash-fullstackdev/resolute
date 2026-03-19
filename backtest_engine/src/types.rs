/// Core data types for the backtest engine.

#[derive(Clone, Debug, Default)]
pub struct CandleArray {
    pub open: Vec<f64>,
    pub high: Vec<f64>,
    pub low: Vec<f64>,
    pub close: Vec<f64>,
    pub volume: Vec<f64>,
    pub timestamp: Vec<f64>,
}

impl CandleArray {
    pub fn len(&self) -> usize {
        self.close.len()
    }

    pub fn is_empty(&self) -> bool {
        self.close.is_empty()
    }

    pub fn slice(&self, end: usize) -> CandleSlice {
        let end = end.min(self.len());
        CandleSlice {
            open: &self.open[..end],
            high: &self.high[..end],
            low: &self.low[..end],
            close: &self.close[..end],
            volume: &self.volume[..end],
            timestamp: &self.timestamp[..end],
        }
    }
}

pub struct CandleSlice<'a> {
    pub open: &'a [f64],
    pub high: &'a [f64],
    pub low: &'a [f64],
    pub close: &'a [f64],
    pub volume: &'a [f64],
    pub timestamp: &'a [f64],
}

#[derive(Clone, Debug, Default)]
pub struct Position {
    pub id: u64,
    pub strategy_idx: usize,
    pub direction: i8, // 1 = long, -1 = short
    pub entry_bar_idx: usize,
    pub entry_price: f64,
    pub quantity: i32,
    pub stop_loss: f64,
    pub target: f64,
    pub time_stop_bar: usize, // 1m bar index for time-based exit
    pub strategy_name: String,
    pub tag: String,
    pub lot_size: u32,
    pub entry_cost: f64, // slippage + brokerage at entry
}

#[derive(Clone, Debug)]
pub struct TradeRecord {
    pub strategy_name: String,
    pub direction: i8,
    pub entry_bar_ts: f64,
    pub exit_bar_ts: f64,
    pub entry_price: f64,
    pub exit_price: f64,
    pub stop_loss: f64,
    pub target: f64,
    pub hold_candles: usize,
    pub quantity: i32,
    pub lot_size: u32,
    pub pnl: f64,
    pub slippage: f64,
    pub brokerage: f64,
    pub exit_reason: String,
    pub tag: String,
}

#[derive(Clone, Debug)]
pub struct EquitySnapshot {
    pub timestamp: f64,
    pub equity: f64,
    pub drawdown_pct: f64,
}

#[derive(Clone, Debug)]
pub struct RawSignal {
    pub direction: i8,          // 1 = buy, -1 = sell
    pub entry_price: f64,
    pub stop_loss: f64,
    pub target: f64,
    pub quantity: i32,
    pub time_stop_bars: u32,    // bars from now (in 1m units)
    pub tag: String,
}

#[derive(Clone, Debug)]
pub struct StrategyEngineConfig {
    pub name: String,
    pub primary_tf_minutes: u32,
    pub active_start_minutes: u32,  // minutes since midnight IST
    pub active_end_minutes: u32,
    pub square_off_minutes: u32,
    pub capital_allocation: f64,
    pub max_positions: u32,
    pub max_drawdown_pct: f64,
    pub max_loss_per_day: f64,
    pub lot_size: u32,
}

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub initial_capital: f64,
    pub slippage_mode: String,      // "fixed" | "percentage"
    pub slippage_value: f64,
    pub brokerage_per_trade: f64,
    pub brokerage_pct: f64,
    pub ambiguous_bar_resolution: String, // "worst_case" | "optimistic"
}

#[derive(Clone, Debug)]
pub struct RawBacktestResult {
    pub trades: Vec<TradeRecord>,
    pub equity_snapshots: Vec<EquitySnapshot>,
    pub per_strategy_equity: Vec<Vec<EquitySnapshot>>,
    pub per_strategy_trades: Vec<Vec<TradeRecord>>,
    pub strategy_names: Vec<String>,
    pub start_ts: f64,
    pub end_ts: f64,
    pub initial_capital: f64,
}
