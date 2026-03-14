"""
LongStrangleStrategy -- buy OTM CE + OTM PE pre-event.

Cheaper than straddle, needs larger move to be profitable.

Entry conditions:
  1. Regime is PRE_EVENT
  2. IV rank < 50 (even cheaper IV desired for strangle)
  3. Implied move affordable
  4. No existing strangle on same underlying

Legs: Buy 1-OTM CE + Buy 1-OTM PE, same expiry
stop_loss_pct: 40% of total premium
time_stop: event_day 14:00 IST
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="long_strangle")


class LongStrangleStrategy(BaseStrategy):
    name = "long_strangle"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime
        if regime.value != "PRE_EVENT":
            return None

        # Condition 2: IV rank < 50
        if chain.iv_rank >= config.get("iv_rank_max", 50):
            return None

        # Condition 3: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # Get OTM strikes
        otm_steps = config.get("otm_steps", 1)
        ce_strike = self.find_otm_strike(chain, "CE", steps=otm_steps)
        pe_strike = self.find_otm_strike(chain, "PE", steps=otm_steps)

        if ce_strike is None or pe_strike is None:
            return None

        ce_premium = ce_strike.call_ltp
        pe_premium = pe_strike.put_ltp

        if ce_premium <= 0 or pe_premium <= 0:
            return None

        strangle_price = ce_premium + pe_premium
        spot = chain.underlying_price
        if spot <= 0:
            return None

        # Implied move check
        implied_move_pct = (strangle_price / spot) * 100
        expected_move = config.get("expected_move_pct", 4.0)
        if implied_move_pct >= expected_move:
            return None

        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 80.0)

        stop_loss_price = strangle_price * (1.0 - stop_loss_pct / 100.0)
        target_price = strangle_price * (1.0 + target_pct / 100.0)

        # Time stop: event day 14:00 IST = 08:30 UTC
        event_date = config.get("event_date")
        if event_date:
            if isinstance(event_date, str):
                from datetime import date as _date
                event_date = _date.fromisoformat(event_date)
            time_stop = datetime.combine(
                event_date, _time(8, 30), tzinfo=timezone.utc
            )
        else:
            time_stop = datetime.now(timezone.utc).replace(
                hour=8, minute=30, second=0, microsecond=0
            ) + timedelta(days=1)

        ce_leg = Leg(
            option_type="CE",
            strike=ce_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=ce_premium,
        )
        pe_leg = Leg(
            option_type="PE",
            strike=pe_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=pe_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="NEUTRAL",
            legs=[ce_leg, pe_leg],
            entry_price=strangle_price,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=strangle_price,
            expiry=chain.expiry,
            confidence=0.55,
            metadata={
                "ce_strike": ce_strike.strike,
                "pe_strike": pe_strike.strike,
                "strangle_price": strangle_price,
                "implied_move_pct": round(implied_move_pct, 2),
                "iv_rank": chain.iv_rank,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        ce_leg = position.legs[0]
        pe_leg = position.legs[1]

        ce_current = pe_current = None
        for s in current_chain.strikes:
            if abs(s.strike - ce_leg.strike) < 0.01:
                ce_current = s
            if abs(s.strike - pe_leg.strike) < 0.01:
                pe_current = s

        if ce_current is None or pe_current is None:
            return False

        current_strangle = ce_current.call_ltp + pe_current.put_ltp
        entry_strangle = ce_leg.premium + pe_leg.premium

        if entry_strangle <= 0:
            return False

        loss_pct = (entry_strangle - current_strangle) / entry_strangle * 100
        if loss_pct >= config.get("stop_loss_pct", 40.0):
            return True

        gain_pct = (current_strangle - entry_strangle) / entry_strangle * 100
        if gain_pct >= config.get("target_pct", 80.0):
            return True

        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False
