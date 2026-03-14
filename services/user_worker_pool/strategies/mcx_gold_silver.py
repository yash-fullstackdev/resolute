"""
MCXGoldSilverStrategy -- MCX commodity long options (Gold/Silver).

Additional checks:
  - MCX market hours (09:00-23:30 IST)
  - MCX option expiry calendar (expire ~5 days before futures expiry)
  - MCX-specific lot sizes from config

Entry conditions:
  1. Regime is COMMODITY_MACRO
  2. IV rank < 50
  3. No existing position on same commodity
  4. DTE 15-30
  5. Within MCX market hours
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="mcx_gold_silver")

# MCX market hours in UTC: 03:30 (09:00 IST) to 18:00 (23:30 IST)
MCX_OPEN_UTC = _time(3, 30)
MCX_CLOSE_UTC = _time(18, 0)


class MCXGoldSilverStrategy(BaseStrategy):
    name = "mcx_gold_silver"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["MCX"]
    requires_margin = False

    # MCX underlyings this strategy handles
    SUPPORTED_COMMODITIES = {"GOLD", "GOLDM", "SILVER", "SILVERM"}

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "MCX")

        # Must be a supported MCX commodity
        if underlying not in self.SUPPORTED_COMMODITIES:
            return None

        # Condition 1: Regime
        if regime.value != "COMMODITY_MACRO":
            return None

        # Condition 2: IV rank < 50
        if chain.iv_rank >= config.get("iv_rank_max", 50):
            return None

        # Condition 3: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # Condition 4: DTE 15-30
        dte = self.get_dte(chain)
        if dte < 15 or dte > 30:
            return None

        # Condition 5: MCX market hours check
        now = datetime.now(timezone.utc)
        current_time = now.time()
        if current_time < MCX_OPEN_UTC or current_time > MCX_CLOSE_UTC:
            return None

        # Direction from config (MCX trends are driven by global macro)
        direction_bias = config.get("direction_bias", "BULLISH")

        if direction_bias == "BULLISH":
            option_type = "CE"
        elif direction_bias == "BEARISH":
            option_type = "PE"
        else:
            return None

        # Strike: ATM or 1-OTM based on IV
        if chain.iv_rank < 25:
            strike_data = self.find_atm_strike(chain, option_type)
        else:
            strike_data = self.find_otm_strike(chain, option_type, steps=1)

        if strike_data is None:
            return None

        premium = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
        if premium <= 0:
            return None

        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 60.0)

        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        # Time stop: 5 days before expiry at MCX close
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=5),
            _time(18, 0),
            tzinfo=timezone.utc,
        )

        leg = Leg(
            option_type=option_type,
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
            direction=direction_bias,
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
                "commodity": underlying,
                "strike": strike_data.strike,
                "premium": premium,
                "iv_rank": chain.iv_rank,
                "direction_bias": direction_bias,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes:
            return False

        # MCX hours check -- if outside hours, do not exit (market closed)
        now = datetime.now(timezone.utc)
        current_time = now.time()
        if current_time < MCX_OPEN_UTC or current_time > MCX_CLOSE_UTC:
            return False

        leg = position.legs[0]
        strike_data = None
        for s in current_chain.strikes:
            if abs(s.strike - leg.strike) < 0.01:
                strike_data = s
                break

        if strike_data is None:
            return False

        current_premium = (
            strike_data.call_ltp if leg.option_type == "CE" else strike_data.put_ltp
        )
        entry_premium = leg.premium
        if entry_premium <= 0:
            return False

        loss_pct = (entry_premium - current_premium) / entry_premium * 100
        if loss_pct >= config.get("stop_loss_pct", 40.0):
            return True

        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        if gain_pct >= config.get("target_pct", 60.0):
            return True

        if now >= position.time_stop:
            return True

        return False
