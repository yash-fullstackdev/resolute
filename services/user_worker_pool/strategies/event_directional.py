"""
EventDirectionalStrategy -- directional play on event day.

Consults EventCalendar for upcoming events (RBI, Budget, OPEC, earnings).
If event within 0-1 days:
  Enter on event morning (after 09:30 IST)
  Use event's expected direction from config (BULLISH/BEARISH/NEUTRAL)
  NEUTRAL -> delegate to long_straddle (return None here)
  BULLISH -> ATM call
  BEARISH -> ATM put

time_stop: 14:30 IST on event day (hard -- never hold past this)
stop_loss_pct: 100% (full premium -- event plays are binary)
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, date as _date, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="event_directional")


class EventDirectionalStrategy(BaseStrategy):
    name = "event_directional"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Must be event day or day before
        if regime.value != "PRE_EVENT":
            return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # Check event direction from config
        event_direction = config.get("event_direction", "NEUTRAL")

        # NEUTRAL events should use long_straddle, not this strategy
        if event_direction == "NEUTRAL":
            return None

        # Check time: must be after 09:30 IST = 04:00 UTC
        now = datetime.now(timezone.utc)
        if now.hour < 4:
            return None

        # Determine option type
        if event_direction == "BULLISH":
            option_type = "CE"
            direction = "BULLISH"
        else:
            option_type = "PE"
            direction = "BEARISH"

        atm_strike = self.find_atm_strike(chain, option_type)
        if atm_strike is None:
            return None

        if option_type == "CE":
            premium = atm_strike.call_ltp
        else:
            premium = atm_strike.put_ltp

        if premium <= 0:
            return None

        # Event plays are binary: stop_loss_pct = 100% (willing to lose full premium)
        stop_loss_pct = config.get("stop_loss_pct", 100.0)
        target_pct = config.get("target_pct", 100.0)

        stop_loss_price = 0.0  # full premium at risk
        target_price = premium * (1.0 + target_pct / 100.0)

        # Time stop: today 14:30 IST = 09:00 UTC (hard)
        event_date = config.get("event_date")
        if event_date:
            if isinstance(event_date, str):
                event_date = _date.fromisoformat(event_date)
            time_stop = datetime.combine(
                event_date, _time(9, 0), tzinfo=timezone.utc
            )
        else:
            time_stop = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if time_stop <= now:
                time_stop += timedelta(days=1)

        leg = Leg(
            option_type=option_type,
            strike=atm_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction=direction,
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=0.5,
            metadata={
                "event_direction": event_direction,
                "strike": atm_strike.strike,
                "premium": premium,
            },
        )

    def should_exit(self, position, current_chain, config):
        # Time stop is the primary exit for event plays
        now = datetime.now(timezone.utc)
        if now >= position.time_stop:
            return True

        if not current_chain.strikes:
            return False

        leg = position.legs[0]
        strike_data = None
        for s in current_chain.strikes:
            if abs(s.strike - leg.strike) < 0.01:
                strike_data = s
                break

        if strike_data is None:
            return False

        if leg.option_type == "CE":
            current_premium = strike_data.call_ltp
        else:
            current_premium = strike_data.put_ltp

        entry_premium = leg.premium
        if entry_premium <= 0:
            return False

        # Target check
        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        if gain_pct >= config.get("target_pct", 100.0):
            return True

        return False
