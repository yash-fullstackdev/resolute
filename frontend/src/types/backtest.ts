// Backtest types

export interface BacktestInstrument {
  name: string;
  display_name: string;
  start_date: string;
  end_date: string;
  trading_days: number;
}

export interface BacktestStrategyOption {
  id?: string;
  name: string;
  display_name: string;
  category: string;
  min_capital_tier: string;
  complexity: string;
  description: string;
}

// ── Multi-strategy config (new) ─────────────────────────────────────────────

export interface BiasFilter {
  type: string;
  timeframe: number;
  params: Record<string, number>;
}

export interface BiasConfig {
  bias_filters: BiasFilter[];
  min_agreement: number;
  cooldown_bars?: number;
  mode?: "bias_filtered" | "independent";
}

export interface StrategySlot {
  name: string;
  session: "morning" | "afternoon" | "all";
  mode: "bias_filtered" | "independent";
  concurrent: boolean;
  max_fires_per_day: number;
  time_stop_bars: number;
  params: Record<string, number>;
  bias_config?: BiasConfig;
}

export interface ExitConfig {
  sl_atr_mult: number;
  tp_atr_mult: number;
  max_hold_bars: number;
  slippage_pts: number;
}

export interface MultiBacktestRequest {
  instrument: string;
  start_date: string;
  end_date: string;
  bias_config: BiasConfig;
  strategies: StrategySlot[];
  exit_config: ExitConfig;
}

// ── Legacy single-strategy config (kept for backward compat) ─────────────────

export interface StrategyRunConfig {
  strategy_name: string;
  instance_name?: string;
  params: Record<string, number | string | boolean>;
  primary_timeframe: number;
  capital_allocation: number;
  active_start: string;
  active_end: string;
  square_off_time: string;
  max_positions: number;
  max_drawdown_pct: number;
  max_loss_per_day: number;
  max_hold_bars: number;
}

export interface BacktestRunRequest {
  instruments: string[];
  start_date: string;
  end_date: string;
  initial_capital: number;
  strategies: StrategyRunConfig[];
  slippage_mode: string;
  slippage_value: number;
  brokerage_preset: string;
  lot_sizes: Record<string, number>;
}

// ── Result types ─────────────────────────────────────────────────────────────

export interface BacktestMetrics {
  total_return_pct: number;
  total_return_inr: number;     // total P&L in points (reused field name)
  final_capital: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  max_drawdown_inr: number;     // max drawdown in points
  sharpe_ratio: number;
  sortino_ratio: number;
  calmar_ratio: number;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  avg_win_inr: number;          // avg win in points
  avg_loss_inr: number;         // avg loss in points
  avg_win_loss_ratio: number;
  max_consecutive_wins: number;
  max_consecutive_losses: number;
  best_day_inr: number;         // best day in points
  worst_day_inr: number;        // worst day in points
}

export interface EquityPoint {
  timestamp: number;
  date: string;
  equity: number;
  drawdown_pct: number;
}

export interface TradeRecord {
  strategy_name: string;
  direction: number;
  direction_label: string;
  entry_price: number;
  exit_price: number;
  stop_loss?: number;
  target?: number;
  sl_pts?: number;
  tp_pts?: number;
  rr_ratio?: string;
  hold_candles?: number;
  exit_bar_ts: number;
  date: string;
  time: string;
  quantity: number;
  lot_size: number;
  pnl: number;
  pnl_rounded: number;
  pnl_pts?: number;
  slippage: number;
  brokerage: number;
  exit_reason: string;
  tag: string;
}

export interface MonthlyPnlPoint {
  month: string;
  pnl: number;
}

export interface DailyPnlPoint {
  date: string;
  pnl: number;
}

export interface BacktestResult {
  metrics: BacktestMetrics;
  per_strategy_metrics: Record<string, BacktestMetrics>;
  equity_curve: EquityPoint[];
  per_strategy_equity: Record<string, EquityPoint[]>;
  trades: TradeRecord[];
  monthly_pnl: MonthlyPnlPoint[];
  daily_pnl: DailyPnlPoint[];
  strategy_names: string[];
  initial_capital: number;
  start_ts: number;
  end_ts: number;
}
