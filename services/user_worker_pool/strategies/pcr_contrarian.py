"""
PCRContrarianStrategy -- contrarian entries on extreme PCR readings.

Indices only.

BULLISH SIGNAL:
  PCR < pcr_extreme_low (default 0.70)
  PCR has been below threshold for >= pcr_persistence_sessions (default 2)
  Underlying near 20-period low (proxy for support)

BEARISH SIGNAL:
  PCR > pcr_extreme_high (default 1.50)
  Same persistence requirement
  Underlying near 20-period high (proxy for resistance)

Strike: ATM
DTE: 5-7 days (weekly expiry)
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="pcr_contrarian")


class PCRContrarianStrategy(BaseStrategy):
    name = "pcr_contrarian"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX"]  # Indices only
    requires_margin = False

    def __init__(self):
        super().__init__()
        # Track PCR persistence across evaluations
        self._pcr_extreme_sessions: dict[str, int] = {}  # underlying -> count

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Indices only
        if segment != "NSE_INDEX":
            return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 5-7
        dte = self.get_dte(chain)
        if dte < 5 or dte > 7:
            return None

        pcr = chain.pcr_oi
        pcr_extreme_low = config.get("pcr_extreme_low", 0.70)
        pcr_extreme_high = config.get("pcr_extreme_high", 1.50)
        persistence_required = config.get("pcr_persistence_sessions", 2)

        direction = None
        option_type = None

        if pcr < pcr_extreme_low:
            # Excessively bearish PCR -> contrarian bullish
            key = f"{underlying}_low"
            self._pcr_extreme_sessions[key] = self._pcr_extreme_sessions.get(key, 0) + 1
            if self._pcr_extreme_sessions[key] < persistence_required:
                return None
            direction = "BULLISH"
            option_type = "CE"
            # Reset the opposite counter
            self._pcr_extreme_sessions.pop(f"{underlying}_high", None)

        elif pcr > pcr_extreme_high:
            # Excessively bullish PCR -> contrarian bearish
            key = f"{underlying}_high"
            self._pcr_extreme_sessions[key] = self._pcr_extreme_sessions.get(key, 0) + 1
            if self._pcr_extreme_sessions[key] < persistence_required:
                return None
            direction = "BEARISH"
            option_type = "PE"
            self._pcr_extreme_sessions.pop(f"{underlying}_low", None)

        else:
            # PCR not at extreme -- reset counters
            self._pcr_extreme_sessions.pop(f"{underlying}_low", None)
            self._pcr_extreme_sessions.pop(f"{underlying}_high", None)
            return None

        # Strike selection: ATM
        atm_strike = self.find_atm_strike(chain, option_type)
        if atm_strike is None:
            return None

        if option_type == "CE":
            premium = atm_strike.call_ltp
        else:
            premium = atm_strike.put_ltp

        if premium <= 0:
            return None

        stop_loss_pct = config.get("stop_loss_pct", 35.0)
        target_pct = config.get("target_pct", 50.0)

        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        # Time stop: Thursday 10:00 IST = 04:30 UTC (weekly expiry day)
        now = datetime.now(timezone.utc)
        days_until_thursday = (3 - now.weekday()) % 7
        if days_until_thursday == 0 and now.hour >= 4:
            days_until_thursday = 7
        time_stop = (now + timedelta(days=days_until_thursday)).replace(
            hour=4, minute=30, second=0, microsecond=0
        )

        leg = Leg(
            option_type=option_type,
            strike=atm_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        # Reset persistence counter after signal generated
        self._pcr_extreme_sessions.pop(f"{underlying}_low", None)
        self._pcr_extreme_sessions.pop(f"{underlying}_high", None)

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
            confidence=0.55,
            metadata={
                "pcr": pcr,
                "direction": direction,
                "strike": atm_strike.strike,
                "premium": premium,
                "persistence_sessions": persistence_required,
            },
        )

    def should_exit(self, position, current_chain, config):
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

        loss_pct = (entry_premium - current_premium) / entry_premium * 100
        if loss_pct >= config.get("stop_loss_pct", 35.0):
            return True

        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        if gain_pct >= config.get("target_pct", 50.0):
            return True

        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False
