export type CapitalTier = "STARTER" | "GROWTH" | "PRO" | "INSTITUTIONAL";

export type StrategyCategory = "BUYING" | "SELLING" | "HYBRID" | "TECHNICAL";

export interface StrategyParam {
  name: string;
  type: "number" | "string" | "boolean" | "select";
  default_value: string | number | boolean;
  current_value: string | number | boolean;
  description: string;
  options?: string[];
  min?: number;
  max?: number;
}

export interface StrategyBiasFilter {
  type: string;
  timeframe: number;
  params: Record<string, number>;
}

export interface StrategyBiasConfig {
  bias_filters: StrategyBiasFilter[];
  min_agreement: number;
  mode: "bias_filtered" | "independent";
}

export interface InstanceExitConfig {
  sl_atr_mult: number;
  tp_atr_mult: number;
  max_hold_bars: number;
  slippage_pts: number;
}

export interface StrategyInstance {
  instance_id: string;
  instance_name: string;
  enabled: boolean;
  mode: "live" | "paper" | "disabled";
  session: "morning" | "afternoon" | "all";
  max_daily_loss_pts: number | null;
  instruments: string[];
  params: Record<string, number | string>;
  bias_config?: StrategyBiasConfig;
  exit_config?: InstanceExitConfig;
  updated_at?: string;
}

export interface Strategy {
  id: string;
  name: string;
  display_name: string;
  description: string;
  category: StrategyCategory;
  min_capital_tier: CapitalTier;
  enabled: boolean;
  params: StrategyParam[];
  instances: StrategyInstance[];
  instruments?: string[];
  bias_config?: StrategyBiasConfig;
  win_rate?: number;
  avg_return?: number;
  total_trades?: number;
  is_custom: boolean;
}

export type IndicatorCategory = "TREND" | "MOMENTUM" | "VOLATILITY" | "VOLUME" | "CUSTOM";

export interface IndicatorConfig {
  name: string;
  display_name: string;
  category: IndicatorCategory;
  params: Record<string, number>;
  default_params: Record<string, number>;
  description: string;
}

export type ConditionOperator =
  | "GREATER_THAN"
  | "LESS_THAN"
  | "EQUALS"
  | "CROSSES_ABOVE"
  | "CROSSES_BELOW"
  | "BETWEEN";

export interface Condition {
  id: string;
  left_operand: string;
  operator: ConditionOperator;
  right_operand: string | number;
  group: number;
}

export interface OptionConfig {
  action: "BUY_CALL" | "BUY_PUT" | "SELL_CALL" | "SELL_PUT";
  strike_selection: "ATM" | "ITM_1" | "ITM_2" | "OTM_1" | "OTM_2" | "OTM_3";
  min_dte: number;
  max_dte: number;
  stop_loss_pct: number;
  target_pct: number;
  time_stop: string;
}

export interface CustomStrategyDefinition {
  id: string;
  name: string;
  description: string;
  indicators: IndicatorConfig[];
  entry_conditions: Condition[];
  exit_conditions: Condition[];
  symbols: string[];
  option_config: OptionConfig;
  status: "DRAFT" | "BACKTESTING" | "REVIEWED" | "ACTIVE" | "PAUSED";
  backtest_results?: BacktestResults;
  ai_review?: string;
  created_at: string;
  updated_at: string;
}

export interface BacktestResults {
  total_trades: number;
  win_rate: number;
  avg_return_pct: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  total_pnl: number;
  profit_factor: number;
  daily_pnl: Array<{ date: string; pnl: number }>;
}
