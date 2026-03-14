"""
RatioBackSpreadStrategy -- sell 1 ATM, buy 2 OTM.

HYBRID category (GROWTH tier, 50k+).

Entry conditions:
  1. Regime is PRE_EVENT or BEAR_RISING_VOL (expecting big move)
  2. IV rank < 50 (buying more options than selling -- want cheap options)
  3. Strong directional conviction from regime + PCR

BULLISH variant: Sell 1 ATM CE, Buy 2 OTM CE
BEARISH variant: Sell 1 ATM PE, Buy 2 OTM PE

Profit profile: limited loss if underlying stays flat; large profit on big move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="ratio_back_spread")


class RatioBackSpreadStrategy(BaseStrategy):
    name = "ratio_back_spread"
    category = StrategyCategory.HYBRID
    min_capital_tier = CapitalTier.GROWTH
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime must be PRE_EVENT or BEAR_RISING_VOL
        if regime.value not in {"PRE_EVENT", "BEAR_RISING_VOL"}:
            return None

        # Condition 2: IV rank < 50
        iv_rank_max = config.get("iv_rank_max", 50)
        if chain.iv_rank >= iv_rank_max:
            logger.debug(
                "ratio_back_spread_skip_high_iv",
                underlying=underlying,
                iv_rank=chain.iv_rank,
            )
            return None

        # Condition 3: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 10-25 DTE
        dte = self.get_dte(chain)
        if dte < 10 or dte > 25:
            return None

        # Determine direction from regime + PCR
        # BEAR_RISING_VOL with high PCR -> bearish, PRE_EVENT -> use PCR to decide
        pcr = chain.pcr_oi
        otm_steps = config.get("ratio_otm_strikes", 2)

        if regime.value == "BEAR_RISING_VOL" or pcr > 1.2:
            # Bearish ratio back spread
            direction = "BEARISH"
            option_type = "PE"
            atm_strike = self.find_atm_strike(chain, "PE")
            otm_strike = self.find_otm_strike(chain, "PE", steps=otm_steps)
        else:
            # Bullish ratio back spread
            direction = "BULLISH"
            option_type = "CE"
            atm_strike = self.find_atm_strike(chain, "CE")
            otm_strike = self.find_otm_strike(chain, "CE", steps=otm_steps)

        if atm_strike is None or otm_strike is None:
            return None

        # Get premiums
        if option_type == "CE":
            sell_premium = atm_strike.call_ltp
            buy_premium = otm_strike.call_ltp
        else:
            sell_premium = atm_strike.put_ltp
            buy_premium = otm_strike.put_ltp

        if sell_premium <= 0 or buy_premium <= 0:
            return None

        # Net cost = 2 * buy_premium - 1 * sell_premium
        net_cost = (2 * buy_premium) - sell_premium
        # Could be debit or small credit
        entry_price = abs(net_cost) if net_cost > 0 else 0.01  # small credit

        # Max loss = net debit + distance between strikes (if flat)
        strike_diff = abs(otm_strike.strike - atm_strike.strike)
        max_loss_inr = max(net_cost, 0) + strike_diff  # worst case near sold strike

        # Stop-loss and target
        stop_loss_pct = config.get("stop_loss_pct", 50.0)
        target_pct = config.get("target_pct", 150.0)

        stop_loss_price = entry_price * (1.0 - stop_loss_pct / 100.0) if entry_price > 0 else 0
        target_price = entry_price * (1.0 + target_pct / 100.0) if entry_price > 0 else 0

        # Time stop: 3 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=3),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        sell_leg = Leg(
            option_type=option_type,
            strike=atm_strike.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_premium,
        )
        buy_leg = Leg(
            option_type=option_type,
            strike=otm_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=2,
            premium=buy_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction=direction,
            legs=[sell_leg, buy_leg],
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss_inr,
            expiry=chain.expiry,
            confidence=0.55,
            metadata={
                "direction": direction,
                "option_type": option_type,
                "sell_strike": atm_strike.strike,
                "buy_strike": otm_strike.strike,
                "sell_premium": sell_premium,
                "buy_premium": buy_premium,
                "net_cost": net_cost,
                "strike_diff": strike_diff,
                "iv_rank": chain.iv_rank,
                "pcr_oi": pcr,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        sell_leg = None
        buy_leg = None
        for leg in position.legs:
            if leg.action == "SELL":
                sell_leg = leg
            elif leg.action == "BUY":
                buy_leg = leg

        if sell_leg is None or buy_leg is None:
            return False

        # Find current premiums
        sell_current = None
        buy_current = None
        for s in current_chain.strikes:
            if abs(s.strike - sell_leg.strike) < 0.01:
                sell_current = s
            if abs(s.strike - buy_leg.strike) < 0.01:
                buy_current = s

        if sell_current is None or buy_current is None:
            return False

        option_type = sell_leg.option_type
        if option_type == "CE":
            sell_now = sell_current.call_ltp
            buy_now = buy_current.call_ltp
        else:
            sell_now = sell_current.put_ltp
            buy_now = buy_current.put_ltp

        # Current value = 2 * buy_now - sell_now (from perspective of holder)
        current_value = (buy_leg.lots * buy_now) - (sell_leg.lots * sell_now)
        entry_value = (buy_leg.lots * buy_leg.premium) - (sell_leg.lots * sell_leg.premium)

        entry_cost = abs(entry_value) if entry_value > 0 else 0.01
        if entry_cost <= 0:
            entry_cost = 0.01

        # Stop loss
        loss_pct = (entry_cost - current_value) / entry_cost * 100 if entry_value > 0 else 0
        if entry_value > 0 and loss_pct >= config.get("stop_loss_pct", 50.0):
            return True

        # Target
        gain_pct = (current_value - entry_cost) / entry_cost * 100 if entry_value > 0 else 0
        if entry_value > 0 and gain_pct >= config.get("target_pct", 150.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Margin for ratio back spread: short leg margin minus long leg offset.

        Conservative estimate: 1 ATM strike worth of margin.
        """
        otm_steps = config.get("ratio_otm_strikes", 2)
        atm = self.find_atm_strike(chain, "CE")
        otm = self.find_otm_strike(chain, "CE", steps=otm_steps)
        if atm and otm:
            return abs(otm.strike - atm.strike)
        return 0.0
