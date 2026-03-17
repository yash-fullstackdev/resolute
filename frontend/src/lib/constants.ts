export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";
export const AUTH_URL = process.env.NEXT_PUBLIC_AUTH_URL ?? "http://localhost:8001/auth/v1";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "http://localhost:8000";

export const STRATEGY_NAMES: Record<string, string> = {
  long_call: "Long Call",
  long_put: "Long Put",
  long_straddle: "Long Straddle",
  long_strangle: "Long Strangle",
  short_straddle: "Short Straddle",
  short_strangle: "Short Strangle",
  iron_condor: "Iron Condor",
  iron_butterfly: "Iron Butterfly",
  bull_call_spread: "Bull Call Spread",
  bear_put_spread: "Bear Put Spread",
  pcr_contrarian: "PCR Contrarian",
  event_directional: "Event Directional",
};

export const TIER_NAMES: Record<string, string> = {
  STARTER: "Starter",
  GROWTH: "Growth",
  PRO: "Pro",
  INSTITUTIONAL: "Institutional",
};

export const TIER_ORDER: Record<string, number> = {
  STARTER: 0,
  GROWTH: 1,
  PRO: 2,
  INSTITUTIONAL: 3,
};

export const TIER_COLORS: Record<string, string> = {
  STARTER: "bg-slate-600 text-slate-200",
  GROWTH: "bg-blue-600 text-blue-100",
  PRO: "bg-purple-600 text-purple-100",
  INSTITUTIONAL: "bg-amber-600 text-amber-100",
};

export const INDICATOR_NAMES: Record<string, string> = {
  RSI: "Relative Strength Index",
  MACD: "MACD",
  SUPERTREND: "SuperTrend",
  BOLLINGER: "Bollinger Bands",
  EMA: "Exponential Moving Average",
  SMA: "Simple Moving Average",
  ATR: "Average True Range",
  ADX: "Average Directional Index",
  VWAP: "VWAP",
  OBV: "On Balance Volume",
  STOCHASTIC: "Stochastic Oscillator",
  CCI: "Commodity Channel Index",
  WILLIAMS_R: "Williams %R",
  MFI: "Money Flow Index",
  PCR: "Put-Call Ratio",
  IV_PERCENTILE: "IV Percentile",
  IV_RANK: "IV Rank",
};

export const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"];

export const REGIME_COLORS: Record<string, string> = {
  TRENDING_UP: "text-profit bg-profit/10",
  TRENDING_DOWN: "text-loss bg-loss/10",
  RANGING: "text-amber-400 bg-amber-400/10",
  VOLATILE: "text-purple-400 bg-purple-400/10",
  UNKNOWN: "text-slate-400 bg-slate-400/10",
};
