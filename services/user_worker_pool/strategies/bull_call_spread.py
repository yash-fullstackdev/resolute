"""
BullCallSpreadStrategy -- buy lower strike call + sell higher strike call.

HYBRID category (GROWTH tier, 50k+).  Defined-risk bullish spread.

Entry conditions:
  1. Regime is BULL_LOW_VOL or BULL_HIGH_VOL
  2. IV rank 30-70 (spreads benefit from moderate IV)
  3. No existing bull_call_spread on same underlying
  4. DTE 15-30 (needs time for spread to work)
  5. PCR < 1.0

Spread construction:
  - Buy ATM call
  - Sell call at ATM + spread_width (from config, default 2 strikes OTM)
  - Max loss = net debit
  - Max profit = spread_width - net_debit
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="bull_call_spread")


class BullCallSpreadStrategy(BaseStrategy):
    name = "bull_call_spread"
    category = StrategyCategory.HYBRID
    min_capital_tier = CapitalTier.GROWTH
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True  # short leg requires margin

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime
        if regime.value not in {"BULL_LOW_VOL", "BULL_HIGH_VOL"}:
            return None

        # Condition 2: IV rank 30-70
        iv_rank_min = config.get("iv_rank_min", 30)
        iv_rank_max = config.get("iv_rank_max", 70)
        if chain.iv_rank < iv_rank_min or chain.iv_rank > iv_rank_max:
            return None

        # Condition 3: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # Condition 4: DTE 15-30
        dte = self.get_dte(chain)
        if dte < 15 or dte > 30:
            return None

        # Condition 5: PCR < 1.0
        if chain.pcr_oi >= config.get("pcr_max", 1.0):
            return None

        # -- Spread construction
        spread_width_steps = config.get("spread_width_steps", 2)

        buy_strike = self.find_atm_strike(chain, "CE")
        sell_strike = self.find_otm_strike(chain, "CE", steps=spread_width_steps)

        if buy_strike is None or sell_strike is None:
            return None
        if sell_strike.strike <= buy_strike.strike:
            return None

        buy_premium = buy_strike.call_ltp
        sell_premium = sell_strike.call_ltp

        if buy_premium <= 0 or sell_premium <= 0:
            return None

        net_debit = buy_premium - sell_premium
        if net_debit <= 0:
            return None

        spread_width_inr = sell_strike.strike - buy_strike.strike
        max_profit = spread_width_inr - net_debit

        if max_profit <= 0:
            return None

        # Stop/target
        stop_loss_pct = config.get("stop_loss_pct", 45.0)
        target_pct = config.get("target_pct", 80.0)

        stop_loss_price = net_debit * (1.0 - stop_loss_pct / 100.0)
        target_price = net_debit * (1.0 + target_pct / 100.0)

        # Time stop: 3 DTE before expiry
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=3),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        buy_leg = Leg(
            option_type="CE",
            strike=buy_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_premium,
        )
        sell_leg = Leg(
            option_type="CE",
            strike=sell_strike.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="BULLISH",
            legs=[buy_leg, sell_leg],
            entry_price=net_debit,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=net_debit,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "buy_strike": buy_strike.strike,
                "sell_strike": sell_strike.strike,
                "net_debit": net_debit,
                "max_profit": max_profit,
                "spread_width": spread_width_inr,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        buy_leg = position.legs[0]
        sell_leg = position.legs[1]

        buy_current = None
        sell_current = None
        for s in current_chain.strikes:
            if abs(s.strike - buy_leg.strike) < 0.01:
                buy_current = s
            if abs(s.strike - sell_leg.strike) < 0.01:
                sell_current = s

        if buy_current is None or sell_current is None:
            return False

        current_spread_value = buy_current.call_ltp - sell_current.call_ltp
        entry_debit = buy_leg.premium - sell_leg.premium

        if entry_debit <= 0:
            return False

        # Stop loss on spread value
        loss_pct = (entry_debit - current_spread_value) / entry_debit * 100
        if loss_pct >= config.get("stop_loss_pct", 45.0):
            return True

        # Target
        gain_pct = (current_spread_value - entry_debit) / entry_debit * 100
        if gain_pct >= config.get("target_pct", 80.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Estimate margin for the short call leg.

        For a bull call spread, margin = spread width (defined risk).
        """
        spread_width_steps = config.get("spread_width_steps", 2)
        atm = self.find_atm_strike(chain, "CE")
        otm = self.find_otm_strike(chain, "CE", steps=spread_width_steps)
        if atm and otm:
            return abs(otm.strike - atm.strike)
        return 0.0
