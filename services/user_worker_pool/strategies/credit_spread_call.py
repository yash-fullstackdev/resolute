"""
CreditSpreadCallStrategy -- bear call credit spread.

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is BEAR_RISING_VOL or SIDEWAYS
  2. IV rank > 40 (selling elevated call premium)
  3. Resistance identified (underlying near 20-day high)
  4. No existing credit_spread_call on same underlying

Legs (same expiry):
  Sell 1 OTM CE (1-2 strikes OTM)
  Buy 1 further OTM CE (spread_width strikes above sold)

Max profit = net credit. Max loss = spread_width x lot_size - credit (DEFINED).
DTE: 15-25. Stop: 2x credit OR 60% max loss. Target: 65%.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="credit_spread_call")


class CreditSpreadCallStrategy(BaseStrategy):
    name = "credit_spread_call"
    category = StrategyCategory.SELLING
    min_capital_tier = CapitalTier.PRO
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime is BEAR_RISING_VOL or SIDEWAYS
        if regime.value not in {"BEAR_RISING_VOL", "SIDEWAYS"}:
            return None

        # Condition 2: IV rank > 40
        iv_rank_min = config.get("iv_rank_min", 40)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: Resistance check -- underlying near 20-day high
        # If chain provides high_20d, check proximity
        if hasattr(chain, "high_20d") and chain.high_20d is not None:
            proximity_pct = (chain.high_20d - chain.underlying_price) / chain.underlying_price * 100
            if proximity_pct > config.get("resistance_proximity_pct", 2.0):
                return None

        # Condition 4: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 15-25
        dte = self.get_dte(chain)
        if dte < 15 or dte > 25:
            return None

        # -- Leg construction
        sell_otm_steps = config.get("sell_otm_strikes", 1)
        spread_width_steps = config.get("spread_width_strikes", 2)

        sell_strike = self.find_otm_strike(chain, "CE", steps=sell_otm_steps)
        buy_strike = self.find_otm_strike(chain, "CE", steps=sell_otm_steps + spread_width_steps)

        if sell_strike is None or buy_strike is None:
            return None
        if buy_strike.strike <= sell_strike.strike:
            return None

        sell_premium = sell_strike.call_ltp
        buy_premium = buy_strike.call_ltp

        if sell_premium <= 0 or buy_premium <= 0:
            return None

        net_credit = sell_premium - buy_premium
        if net_credit <= 0:
            return None

        spread_width_inr = buy_strike.strike - sell_strike.strike
        max_loss = spread_width_inr - net_credit

        if max_loss <= 0:
            return None

        # Stop: 2x credit OR 60% of max loss
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        max_loss_trigger_pct = config.get("max_loss_trigger_pct", 60.0)
        stop_loss_credit = net_credit * credit_loss_multiple
        stop_loss_max_loss = max_loss * (max_loss_trigger_pct / 100.0)
        effective_stop = min(stop_loss_credit, stop_loss_max_loss)
        stop_loss_price = net_credit + effective_stop

        # Profit target: 65% of credit
        profit_target_pct = config.get("profit_target_pct", 65.0)
        target_price = net_credit * (1.0 - profit_target_pct / 100.0)

        # Time stop: 3 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=3),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        sell_leg = Leg(
            option_type="CE",
            strike=sell_strike.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_premium,
        )
        buy_leg = Leg(
            option_type="CE",
            strike=buy_strike.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="BEARISH",
            legs=[sell_leg, buy_leg],
            entry_price=net_credit,
            stop_loss_pct=credit_loss_multiple * 100,
            stop_loss_price=stop_loss_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "sell_strike": sell_strike.strike,
                "buy_strike": buy_strike.strike,
                "net_credit": net_credit,
                "max_loss": max_loss,
                "spread_width": spread_width_inr,
                "iv_rank": chain.iv_rank,
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

        sell_current = None
        buy_current = None
        for s in current_chain.strikes:
            if abs(s.strike - sell_leg.strike) < 0.01:
                sell_current = s
            if abs(s.strike - buy_leg.strike) < 0.01:
                buy_current = s

        if sell_current is None or buy_current is None:
            return False

        current_spread_cost = sell_current.call_ltp - buy_current.call_ltp
        entry_credit = sell_leg.premium - buy_leg.premium

        if entry_credit <= 0:
            return False

        # Exit 1: Credit loss multiple
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        loss = current_spread_cost - entry_credit
        if loss > 0 and loss / entry_credit >= credit_loss_multiple:
            return True

        # Exit 2: Max loss percentage
        spread_width = buy_leg.strike - sell_leg.strike
        max_loss = spread_width - entry_credit
        if max_loss > 0:
            current_loss = current_spread_cost - entry_credit
            if current_loss > 0 and current_loss / max_loss >= config.get("max_loss_trigger_pct", 60.0) / 100.0:
                return True

        # Exit 3: Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        # Exit 4: Profit target
        profit_target_pct = config.get("profit_target_pct", 65.0)
        decay_pct = (entry_credit - current_spread_cost) / entry_credit * 100
        if decay_pct >= profit_target_pct:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Credit spread margin = spread_width x lot_size."""
        spread_width_steps = config.get("spread_width_strikes", 2)
        if len(chain.strikes) > 1:
            strike_gap = chain.strikes[1].strike - chain.strikes[0].strike
        else:
            strike_gap = 50
        lot_size = config.get("lot_size", 50)
        return spread_width_steps * strike_gap * lot_size
