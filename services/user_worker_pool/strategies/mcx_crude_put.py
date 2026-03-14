"""
MCXCrudePutStrategy -- MCX crude protective puts.

Protective put strategy for crude oil exposure.  Buys puts as insurance
against crude price drops.

Entry conditions:
  1. Underlying is CRUDEOIL or CRUDEOILM
  2. Regime is COMMODITY_MACRO or BEAR_HIGH_VOL
  3. IV rank < 60
  4. No existing crude put on same underlying
  5. DTE 15-30
  6. Within MCX market hours

Stop loss: 35% premium loss
Time stop: 7 days before expiry
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="mcx_crude_put")

MCX_OPEN_UTC = _time(3, 30)
MCX_CLOSE_UTC = _time(18, 0)


class MCXCrudePutStrategy(BaseStrategy):
    name = "mcx_crude_put"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["MCX"]
    requires_margin = False

    SUPPORTED_COMMODITIES = {"CRUDEOIL", "CRUDEOILM"}

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "MCX")

        # Must be crude oil
        if underlying not in self.SUPPORTED_COMMODITIES:
            return None

        # Regime
        allowed_regimes = {"COMMODITY_MACRO", "BEAR_HIGH_VOL", "BEAR_LOW_VOL"}
        if regime.value not in allowed_regimes:
            return None

        # IV rank < 60
        if chain.iv_rank >= config.get("iv_rank_max", 60):
            return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE 15-30
        dte = self.get_dte(chain)
        if dte < 15 or dte > 30:
            return None

        # MCX hours
        now = datetime.now(timezone.utc)
        current_time = now.time()
        if current_time < MCX_OPEN_UTC or current_time > MCX_CLOSE_UTC:
            return None

        # Buy ATM put for protection, or 1-OTM for cheaper insurance
        if chain.iv_rank < 30:
            strike_data = self.find_atm_strike(chain, "PE")
        else:
            strike_data = self.find_otm_strike(chain, "PE", steps=1)

        if strike_data is None:
            return None

        premium = strike_data.put_ltp
        if premium <= 0:
            return None

        stop_loss_pct = config.get("stop_loss_pct", 35.0)
        target_pct = config.get("target_pct", 80.0)

        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        # Time stop: 7 days before expiry
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=7),
            _time(18, 0),
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
            confidence=0.55,
            metadata={
                "commodity": underlying,
                "strike": strike_data.strike,
                "premium": premium,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes:
            return False

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

        current_premium = strike_data.put_ltp
        entry_premium = leg.premium
        if entry_premium <= 0:
            return False

        loss_pct = (entry_premium - current_premium) / entry_premium * 100
        if loss_pct >= config.get("stop_loss_pct", 35.0):
            return True

        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        if gain_pct >= config.get("target_pct", 80.0):
            return True

        if now >= position.time_stop:
            return True

        return False
