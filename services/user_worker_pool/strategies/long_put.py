"""
LongPutStrategy -- buy ATM/OTM put on bearish signal.

Mirror of long_call for bearish conditions:
  1. Regime is BEAR_LOW_VOL or BEAR_HIGH_VOL
  2. IV rank < 60
  3. No existing long_put on same underlying
  4. DTE within segment range
  5. PCR > 1.2 (not excessively bullish)
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="long_put")


class LongPutStrategy(BaseStrategy):
    name = "long_put"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    _DTE_RANGES = {
        "NSE_INDEX": (7, 15),
        "NSE_FO": (20, 35),
        "MCX": (15, 30),
    }

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime must be bearish
        bearish_regimes = {"BEAR_LOW_VOL", "BEAR_HIGH_VOL", "BEAR_RISING_VOL"}
        if regime.value not in bearish_regimes:
            return None

        # Condition 2: IV rank < 60
        if chain.iv_rank >= config.get("iv_rank_max", 60):
            return None

        # Condition 3: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # Condition 4: DTE check
        dte = self.get_dte(chain)
        min_dte, max_dte = self._DTE_RANGES.get(segment, (7, 15))
        if dte < min_dte or dte > max_dte:
            return None

        # Condition 5: PCR > 1.2 (market is bullish == contrarian bearish opportunity)
        pcr_min = config.get("pcr_min_bearish", 1.2)
        if chain.pcr_oi < pcr_min:
            return None

        # Strike selection
        if chain.iv_rank < 30:
            strike_data = self.find_atm_strike(chain, "PE")
        else:
            strike_data = self.find_otm_strike(chain, "PE", steps=1)

        if strike_data is None:
            return None

        premium = strike_data.put_ltp
        if premium <= 0:
            return None

        stop_loss_pct = config.get("stop_loss_pct", 38.0)
        target_pct = config.get("target_pct", 60.0)

        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        # Time stop
        now = datetime.now(timezone.utc)
        if segment == "NSE_INDEX":
            days_until_wednesday = (2 - now.weekday()) % 7
            if days_until_wednesday == 0 and now.hour >= 9:
                days_until_wednesday = 7
            time_stop = (now + timedelta(days=days_until_wednesday)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
        else:
            time_stop = datetime.combine(
                chain.expiry - timedelta(days=1),
                _time(9, 0),
                tzinfo=timezone.utc,
            )

        leg = Leg(
            option_type="PE",
            strike=strike_data.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="BEARISH",
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=0.7 if chain.iv_rank < 30 else 0.5,
            metadata={
                "strike": strike_data.strike,
                "premium": premium,
                "iv_rank": chain.iv_rank,
                "pcr_oi": chain.pcr_oi,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes:
            return False

        strike_data = None
        for s in current_chain.strikes:
            if abs(s.strike - position.legs[0].strike) < 0.01:
                strike_data = s
                break

        if strike_data is None:
            return False

        current_premium = strike_data.put_ltp
        entry_premium = position.legs[0].premium

        if entry_premium <= 0:
            return False

        # Stop loss
        loss_pct = (entry_premium - current_premium) / entry_premium * 100
        if loss_pct >= config.get("stop_loss_pct", 38.0):
            return True

        # Target
        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        if gain_pct >= config.get("target_pct", 60.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False
