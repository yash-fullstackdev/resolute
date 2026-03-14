export interface TradingPlan {
  id: string;
  tenant_id: string;
  date: string;
  enabled_strategies: string[];
  active_underlyings: string[];
  max_trades: number;
  daily_loss_limit: number;
  daily_profit_target?: number;
  thesis: string;
  is_locked: boolean;
  locked_at?: string;
  plan_hash?: string;
  created_at: string;
  updated_at: string;
}

export type CircuitBreakerStatus = "ACTIVE" | "HALTED" | "COOLDOWN";

export interface CircuitBreakerState {
  status: CircuitBreakerStatus;
  reason?: string;
  halted_at?: string;
  resume_at?: string;
  daily_loss: number;
  daily_loss_limit: number;
  consecutive_losses: number;
  max_consecutive_losses: number;
}

export type OverrideType = "STOP_LOSS_MOVE" | "TIME_STOP_EXTEND" | "EARLY_EXIT" | "POSITION_SIZE_INCREASE";

export interface OverrideRequest {
  id: string;
  tenant_id: string;
  position_id: string;
  override_type: OverrideType;
  reason: string;
  original_value: string;
  proposed_value: string;
  cooldown_expires_at: string;
  status: "PENDING_COOLDOWN" | "APPROVED" | "EXECUTED" | "EXPIRED";
  pnl_impact?: number;
  created_at: string;
}

export interface JournalEntry {
  id: string;
  tenant_id: string;
  position_id?: string;
  date: string;
  entry_type: "TRADE" | "OBSERVATION" | "LESSON" | "REVIEW";
  title: string;
  content: string;
  tags: string[];
  mood?: "CONFIDENT" | "NEUTRAL" | "ANXIOUS" | "FRUSTRATED";
  discipline_score?: number;
  created_at: string;
  updated_at: string;
}

export interface DisciplineScore {
  score: number;
  components: {
    plan_adherence: number;
    stop_loss_discipline: number;
    position_sizing: number;
    override_penalty: number;
    consistency: number;
  };
  trend: "IMPROVING" | "STABLE" | "DECLINING";
  updated_at: string;
}
