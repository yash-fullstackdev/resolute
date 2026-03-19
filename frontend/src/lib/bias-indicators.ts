/**
 * Shared bias indicator definitions — used by both:
 *   - /strategies page (StrategyConfigModal)
 *   - /backtest page (BacktestConfigPanel)
 *
 * Single source of truth for indicator types, params, and timeframe options.
 */

export const INDICATOR_TYPES: Record<
  string,
  {
    label: string;
    params: {
      key: string;
      label: string;
      default: number;
      min?: number;
      max?: number;
      step?: number;
    }[];
  }
> = {
  ema_crossover: {
    label: "EMA Crossover",
    params: [
      { key: "short", label: "Short Period", default: 9, min: 1, max: 200 },
      { key: "long", label: "Long Period", default: 21, min: 2, max: 500 },
    ],
  },
  supertrend: {
    label: "Supertrend",
    params: [
      { key: "period", label: "Period", default: 10, min: 5, max: 50 },
      { key: "multiplier", label: "Multiplier", default: 3.0, min: 0.5, max: 10, step: 0.1 },
    ],
  },
  rsi_zone: {
    label: "RSI Zone",
    params: [
      { key: "period", label: "Period", default: 14, min: 2, max: 50 },
      { key: "overbought", label: "Overbought", default: 70, min: 50, max: 95 },
      { key: "oversold", label: "Oversold", default: 30, min: 5, max: 50 },
    ],
  },
  ttm_momentum: {
    label: "TTM Squeeze Momentum",
    params: [{ key: "period", label: "Period", default: 20, min: 5, max: 50 }],
  },
  macd_signal: {
    label: "MACD Signal",
    params: [
      { key: "fast", label: "Fast EMA", default: 12, min: 2, max: 50 },
      { key: "slow", label: "Slow EMA", default: 26, min: 5, max: 100 },
      { key: "signal", label: "Signal", default: 9, min: 2, max: 30 },
    ],
  },
  ema_zone: {
    label: "EMA Zone + RSI",
    params: [
      { key: "ema_period", label: "EMA Period", default: 33, min: 5, max: 200 },
      { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
      { key: "rsi_bull", label: "RSI Bull", default: 60, min: 50, max: 90 },
      { key: "rsi_bear", label: "RSI Bear", default: 40, min: 10, max: 50 },
    ],
  },
  price_vs_ema: {
    label: "Price vs EMA",
    params: [{ key: "period", label: "EMA Period", default: 20, min: 2, max: 200 }],
  },
  bollinger_squeeze: {
    label: "Bollinger Squeeze",
    params: [
      { key: "period", label: "Period", default: 20, min: 5, max: 50 },
      { key: "std_mult", label: "Std Dev", default: 2.0, min: 0.5, max: 4, step: 0.1 },
    ],
  },
};

export const TF_OPTIONS = [1, 2, 3, 5, 10, 15, 30, 60];

export interface BiasFilterConfig {
  type: string;
  timeframe: number;
  params: Record<string, number>;
}

export interface StrategyBiasConfig {
  bias_filters: BiasFilterConfig[];
  min_agreement: number;
  mode: "bias_filtered" | "independent";
}

export function getDefaultBiasConfig(): StrategyBiasConfig {
  return {
    bias_filters: [],
    min_agreement: 2,
    mode: "independent",
  };
}

export function getDefaultFilterParams(type: string): Record<string, number> {
  const def = INDICATOR_TYPES[type];
  if (!def) return {};
  const p: Record<string, number> = {};
  for (const d of def.params) p[d.key] = d.default;
  return p;
}
