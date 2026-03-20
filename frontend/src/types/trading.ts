export interface Tick {
  symbol: string;
  last_price: number;
  change_pct: number;
  volume?: number;
  timestamp: string;
}

export interface StrikeData {
  strike_price: number;
  call_ltp: number;
  put_ltp: number;
  call_oi: number;
  put_oi: number;
  call_iv: number;
  put_iv: number;
  call_delta: number;
  put_delta: number;
  call_gamma: number;
  put_gamma: number;
  call_theta: number;
  put_theta: number;
  call_vega: number;
  put_vega: number;
}

export interface OptionsChainSnapshot {
  underlying: string;
  spot_price: number;
  expiry: string;
  timestamp: string;
  strikes: StrikeData[];
  pcr: number;
  total_call_oi: number;
  total_put_oi: number;
}

export type SignalDirection =
  | "BUY_CALL"
  | "BUY_PUT"
  | "SELL_CALL"
  | "SELL_PUT"
  | "BUY_STRADDLE"
  | "SELL_STRADDLE"
  | "BUY_STRANGLE"
  | "SELL_STRANGLE"
  | "IRON_CONDOR"
  | "IRON_BUTTERFLY"
  | "BULL_CALL_SPREAD"
  | "BEAR_PUT_SPREAD"
  | "EXIT";

export type MarketRegime = "TRENDING_UP" | "TRENDING_DOWN" | "RANGING" | "VOLATILE" | "UNKNOWN";

export interface SignalLeg {
  action: "BUY" | "SELL";
  option_type: "CE" | "PE";
  strike: number;
  expiry: string;
  lots: number;
}

export interface SignalOptionsOverlay {
  strike: number;
  option_type: "CE" | "PE";
  ltp: number;
  sl: number;
  tp: number;
  delta: number;
  iv: number | null;
  risk: number;
  reward: number;
  rr: string;
  expiry: string | null;
}

export interface Signal {
  id: string;
  strategy_name: string;
  underlying: string;
  direction: SignalDirection | string;
  strength: number;
  regime: MarketRegime;
  legs: SignalLeg[];
  signal_type: "OPTIONS" | "DIRECT";
  entry_price: number | null;
  stop_loss_price: number | null;
  target_price: number | null;
  index_risk_pts?: number;
  index_reward_pts?: number;
  index_rr?: string;
  has_options_chain?: boolean;
  options?: SignalOptionsOverlay;
  rationale: string;
  current_price?: number | null;
  live_pnl?: number | null;
  trade_status?: string;
  created_at: string;
  executed: boolean;
  metadata?: Record<string, unknown>;
}

export type OrderStatus = "PENDING" | "PLACED" | "FILLED" | "PARTIALLY_FILLED" | "CANCELLED" | "REJECTED" | "ERROR";

export interface Order {
  id: string;
  position_id: string;
  order_type: "ENTRY" | "EXIT" | "STOP_LOSS" | "TARGET";
  symbol: string;
  action: "BUY" | "SELL";
  quantity: number;
  price: number;
  fill_price?: number;
  status: OrderStatus;
  broker_order_id?: string;
  created_at: string;
  updated_at: string;
}

export type PositionStatus = "OPEN" | "CLOSED" | "PARTIALLY_CLOSED" | "ERROR";

export interface PositionLeg {
  symbol: string;
  option_type: "CE" | "PE";
  strike: number;
  expiry: string;
  action: "BUY" | "SELL";
  quantity: number;
  entry_price: number;
  current_price: number;
  pnl: number;
}

export interface Position {
  id: string;
  tenant_id: string;
  strategy_name: string;
  underlying: string;
  direction: SignalDirection;
  status: PositionStatus;
  legs: PositionLeg[];
  entry_time: string;
  exit_time?: string;
  total_pnl: number;
  total_pnl_pct: number;
  unrealized_pnl: number;
  realized_pnl: number;
  stop_loss?: number;
  target?: number;
  capital_deployed: number;
  max_drawdown: number;
  created_at: string;
  updated_at: string;
}
