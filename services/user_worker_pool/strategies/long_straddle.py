"""
LongStraddleStrategy -- buy ATM CE + ATM PE pre-event.

Entry conditions (ALL must be true):
  1. Regime is PRE_EVENT (event within event_lookahead_days, default 2)
  2. IV rank < 55 (buy before IV expansion, not after)
  3. Implied move (straddle_price / spot * 100) < expected_move from config
  4. No existing straddle on same underlying

Legs: Buy ATM CE + Buy ATM PE, same expiry (nearest weekly for index)
stop_loss_pct: 30% of total premium
time_stop: event_day 14:30 IST (hard rule)
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="long_straddle")


class LongStraddleStrategy(BaseStrategy):
    name = "long_straddle"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime must be PRE_EVENT
        if regime.value != "PRE_EVENT":
            return None

        # Condition 2: IV rank < 55
        iv_rank_max = config.get("iv_rank_max", 55)
        if chain.iv_rank >= iv_rank_max:
            return None

        # Condition 3: Implied move check
        atm_strike = self.find_atm_strike(chain, "CE")
        if atm_strike is None:
            return None

        atm_ce_premium = atm_strike.call_ltp
        atm_pe_premium = atm_strike.put_ltp

        if atm_ce_premium <= 0 or atm_pe_premium <= 0:
            return None

        straddle_price = atm_ce_premium + atm_pe_premium
        spot = chain.underlying_price
        if spot <= 0:
            return None

        implied_move_pct = (straddle_price / spot) * 100
        expected_move = config.get("expected_move_pct", 3.0)
        if implied_move_pct >= expected_move:
            # Straddle already priced in the move -- too expensive
            return None

        # Condition 4: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # -- Build signal
        stop_loss_pct = config.get("stop_loss_pct", 30.0)
        target_pct = config.get("target_pct", 50.0)

        stop_loss_price = straddle_price * (1.0 - stop_loss_pct / 100.0)
        target_price = straddle_price * (1.0 + target_pct / 100.0)

        # Time stop: event day 14:30 IST = 09:00 UTC
        # Default to next trading day if event_date not in config
        event_date = config.get("event_date")
        if event_date:
            if isinstance(event_date, str):
                from datetime import date as _date
                event_date = _date.fromisoformat(event_date)
            time_stop = datetime.combine(
                event_date, _time(9, 0), tzinfo=timezone.utc
            )
        else:
            # Default: tomorrow 14:30 IST
            time_stop = datetime.now(timezone.utc).replace(
                hour=9, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

        ce_leg = Leg(
            option_type="CE",
            strike=atm_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=atm_ce_premium,
        )
        pe_leg = Leg(
            option_type="PE",
            strike=atm_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=atm_pe_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="NEUTRAL",
            legs=[ce_leg, pe_leg],
            entry_price=straddle_price,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=straddle_price,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "strike": atm_strike.strike,
                "straddle_price": straddle_price,
                "implied_move_pct": round(implied_move_pct, 2),
                "iv_rank": chain.iv_rank,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        ce_leg = position.legs[0]
        pe_leg = position.legs[1]

        # Find current prices for both legs
        ce_current = pe_current = None
        for s in current_chain.strikes:
            if abs(s.strike - ce_leg.strike) < 0.01:
                ce_current = s
            if abs(s.strike - pe_leg.strike) < 0.01:
                pe_current = s

        if ce_current is None or pe_current is None:
            return False

        current_straddle = ce_current.call_ltp + pe_current.put_ltp
        entry_straddle = ce_leg.premium + pe_leg.premium

        if entry_straddle <= 0:
            return False

        # Stop loss
        loss_pct = (entry_straddle - current_straddle) / entry_straddle * 100
        if loss_pct >= config.get("stop_loss_pct", 30.0):
            return True

        # Target
        gain_pct = (current_straddle - entry_straddle) / entry_straddle * 100
        if gain_pct >= config.get("target_pct", 50.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False
