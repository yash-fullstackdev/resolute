"""
StopLossManager -- enforces all stop-loss, time-stop, and profit-target rules.

Called every tick cycle for each open position.
Handles BUYING strategies (premium loss %) and provides the foundation for
SELLING/HYBRID stop rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as _time, timezone

from ..strategies.base import Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="stop_loss")


@dataclass
class StopResult:
    """Result of a stop-loss check."""
    should_exit: bool
    reason: str  # "STOP_HIT" | "TIME_STOP" | "TARGET_HIT" | "NONE"
    current_value: float = 0.0
    loss_pct: float = 0.0
    gain_pct: float = 0.0


class StopLossManager:
    """Enforce all stop-loss and time-stop rules from the strategy spec."""

    # BUYING strategy stop rules (premium loss based)
    BUYING_STOP_RULES: dict[str, dict] = {
        "long_call":         {"premium_loss_pct": 38, "time_stop": "weekly_wednesday_3pm"},
        "long_put":          {"premium_loss_pct": 38, "time_stop": "weekly_wednesday_3pm"},
        "bull_call_spread":  {"premium_loss_pct": 45, "time_stop": "profit_target_80pct"},
        "bear_put_spread":   {"premium_loss_pct": 45, "time_stop": "profit_target_80pct"},
        "long_straddle":     {"premium_loss_pct": 30, "time_stop": "event_day_1430"},
        "long_strangle":     {"premium_loss_pct": 40, "time_stop": "event_day_1400"},
        "pcr_contrarian":    {"premium_loss_pct": 35, "time_stop": "weekly_thursday_1000"},
        "event_directional": {"premium_loss_pct": 100, "time_stop": "event_day_1430"},
        "mcx_gold_silver":   {"premium_loss_pct": 40, "time_stop": "mcx_5d_before_expiry"},
        "mcx_crude_put":     {"premium_loss_pct": 35, "time_stop": "mcx_7d_before_expiry"},
    }

    HYBRID_STOP_RULES: dict[str, dict] = {
        "iron_butterfly_long": {"premium_loss_pct": 35, "time_stop": "event_day_1430"},
        "diagonal_spread":     {"premium_loss_pct": 40, "time_stop": "5dte_far_expiry"},
        "ratio_back_spread":   {"premium_loss_pct": 50, "time_stop": "3dte"},
    }

    SELLING_STOP_RULES: dict[str, dict] = {
        "short_straddle":     {"credit_loss_multiple": 1.5, "time_stop": "3dte", "profit_target_pct": 50},
        "short_strangle":     {"credit_loss_multiple": 2.0, "time_stop": "5dte", "profit_target_pct": 50},
        "credit_spread_call": {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 65},
        "credit_spread_put":  {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 65},
        "iron_condor":        {"breach_trigger": True,      "time_stop": "5dte", "profit_target_pct": 50},
        "jade_lizard":        {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 50},
        "covered_call":       {"futures_loss_pct": 3.0,     "time_stop": "5dte_roll", "profit_target_pct": 80},
    }

    @property
    def STOP_RULES(self) -> dict[str, dict]:
        return {**self.BUYING_STOP_RULES, **self.HYBRID_STOP_RULES, **self.SELLING_STOP_RULES}

    def check_stop(self, position: Position, current_chain) -> StopResult:
        """Check if position should be stopped out based on premium loss.

        For BUYING: check premium_loss_pct against current position value.
        """
        rules = self.STOP_RULES.get(position.strategy_name)
        if rules is None:
            return StopResult(should_exit=False, reason="NONE")

        # Calculate current position value from chain
        current_value = self._calculate_current_value(position, current_chain)
        if current_value < 0:
            return StopResult(should_exit=False, reason="NONE")

        entry_cost = position.entry_cost_inr
        if entry_cost <= 0:
            return StopResult(should_exit=False, reason="NONE")

        # BUYING strategies: premium loss %
        premium_loss_pct = rules.get("premium_loss_pct")
        if premium_loss_pct is not None:
            loss_pct = (entry_cost - current_value) / entry_cost * 100
            if loss_pct >= premium_loss_pct:
                logger.info(
                    "stop_loss_triggered",
                    position_id=position.position_id,
                    strategy=position.strategy_name,
                    loss_pct=round(loss_pct, 2),
                    threshold=premium_loss_pct,
                )
                return StopResult(
                    should_exit=True,
                    reason="STOP_HIT",
                    current_value=current_value,
                    loss_pct=loss_pct,
                )

        # SELLING strategies: credit loss multiple
        credit_loss_multiple = rules.get("credit_loss_multiple")
        if credit_loss_multiple is not None:
            # For selling: entry_cost is the credit received (positive)
            # current_value is the cost to close (higher = more loss)
            loss = current_value - entry_cost
            if entry_cost > 0 and loss / entry_cost >= credit_loss_multiple:
                logger.info(
                    "credit_stop_triggered",
                    position_id=position.position_id,
                    strategy=position.strategy_name,
                    loss_multiple=round(loss / entry_cost, 2),
                    threshold=credit_loss_multiple,
                )
                return StopResult(
                    should_exit=True,
                    reason="STOP_HIT",
                    current_value=current_value,
                    loss_pct=loss / entry_cost * 100 if entry_cost > 0 else 0,
                )

        return StopResult(
            should_exit=False,
            reason="NONE",
            current_value=current_value,
        )

    def check_time_stop(self, position: Position, now: datetime | None = None) -> bool:
        """Check if the position's time stop has been reached."""
        now = now or datetime.now(timezone.utc)
        if position.time_stop and now >= position.time_stop:
            logger.info(
                "time_stop_triggered",
                position_id=position.position_id,
                strategy=position.strategy_name,
                time_stop=position.time_stop.isoformat(),
            )
            return True
        return False

    def check_profit_target(self, position: Position, current_chain) -> bool:
        """Check if profit target has been reached.

        For BUYING: premium gained > target %.
        For SELLING: position value decayed below target threshold.
        """
        rules = self.STOP_RULES.get(position.strategy_name)
        if rules is None:
            return False

        current_value = self._calculate_current_value(position, current_chain)
        if current_value < 0:
            return False

        entry_cost = position.entry_cost_inr
        if entry_cost <= 0:
            return False

        # BUYING: check gain percentage
        profit_target_pct = rules.get("profit_target_pct")
        if profit_target_pct is None:
            # For buying strategies without explicit profit_target_pct,
            # use the target from the position itself
            if position.target_price > 0 and current_value >= position.target_price:
                logger.info(
                    "profit_target_hit",
                    position_id=position.position_id,
                    current_value=current_value,
                    target=position.target_price,
                )
                return True
            return False

        gain_pct = (current_value - entry_cost) / entry_cost * 100
        if gain_pct >= profit_target_pct:
            logger.info(
                "profit_target_hit",
                position_id=position.position_id,
                strategy=position.strategy_name,
                gain_pct=round(gain_pct, 2),
                threshold=profit_target_pct,
            )
            return True

        return False

    def _calculate_current_value(self, position: Position, current_chain) -> float:
        """Calculate current mark-to-market value of all position legs."""
        if not current_chain.strikes:
            return -1.0

        total_value = 0.0
        for leg in position.legs:
            strike_data = None
            for s in current_chain.strikes:
                if abs(s.strike - leg.strike) < 0.01:
                    strike_data = s
                    break

            if strike_data is None:
                return -1.0

            if leg.option_type == "CE":
                current_premium = strike_data.call_ltp
            else:
                current_premium = strike_data.put_ltp

            if leg.action == "BUY":
                total_value += current_premium * leg.lots
            else:
                # For sold legs, the value to close is the current premium
                total_value -= current_premium * leg.lots

        return total_value
