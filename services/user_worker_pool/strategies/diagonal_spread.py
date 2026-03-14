"""
DiagonalSpreadStrategy -- buy far-expiry ATM call, sell near-expiry OTM call.

HYBRID category (GROWTH tier, 50k+).

Entry conditions:
  1. Regime is SIDEWAYS or BULL_LOW_VOL
  2. IV rank > 30 (need some IV for near-expiry short leg)
  3. VIX < 22 (stable environment for calendars)
  4. Term structure in contango (near IV > far IV preferred)
  5. No existing diagonal_spread on same underlying

Legs:
  Buy 1 far-expiry ATM/1-OTM call (30-45 DTE)
  Sell 1 near-expiry 1-OTM call (7-14 DTE)

Special exit: close if short leg goes ITM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, date, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="diagonal_spread")


class DiagonalSpreadStrategy(BaseStrategy):
    name = "diagonal_spread"
    category = StrategyCategory.HYBRID
    min_capital_tier = CapitalTier.GROWTH
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime is SIDEWAYS or BULL_LOW_VOL
        if regime.value not in {"SIDEWAYS", "BULL_LOW_VOL"}:
            return None

        # Condition 2: IV rank > 30
        iv_rank_min = config.get("iv_rank_min", 30)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: VIX < 22
        vix_max = config.get("vix_max", 22)
        if hasattr(chain, "vix") and chain.vix is not None and chain.vix >= vix_max:
            return None

        # Condition 4: Term structure check -- near IV > far IV (contango)
        # We rely on chain metadata for term structure; skip if not available
        if hasattr(chain, "near_expiry_iv") and hasattr(chain, "far_expiry_iv"):
            if chain.near_expiry_iv < chain.far_expiry_iv:
                logger.debug(
                    "diagonal_skip_backwardation",
                    underlying=underlying,
                    near_iv=chain.near_expiry_iv,
                    far_iv=chain.far_expiry_iv,
                )
                return None

        # Condition 5: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check for the chain (near-expiry leg): 7-14 DTE
        dte = self.get_dte(chain)
        if dte < 7 or dte > 14:
            return None

        # -- Leg construction
        # Near-expiry: sell 1-OTM call
        sell_strike = self.find_otm_strike(chain, "CE", steps=1)
        if sell_strike is None:
            return None

        sell_premium = sell_strike.call_ltp
        if sell_premium <= 0:
            return None

        # Far-expiry: buy ATM call (30-45 DTE)
        # We use the ATM strike price from the current chain; the far-expiry premium
        # would come from a separate chain snapshot. Estimate as ATM + calendar premium.
        buy_strike = self.find_atm_strike(chain, "CE")
        if buy_strike is None:
            return None

        # For the far-expiry leg, premium is higher due to more time value
        # Use a multiplier estimate if far-expiry chain is not available
        far_expiry_dte = config.get("far_expiry_dte", 35)
        far_expiry_date = date.today() + timedelta(days=far_expiry_dte)

        buy_premium = buy_strike.call_ltp
        if buy_premium <= 0:
            return None

        # Calendar premium adjustment: far-expiry costs more
        calendar_multiplier = config.get("calendar_premium_multiplier", 1.8)
        estimated_far_premium = buy_premium * calendar_multiplier

        net_debit = estimated_far_premium - sell_premium
        if net_debit <= 0:
            return None

        # Stop-loss and target
        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 60.0)

        stop_loss_price = net_debit * (1.0 - stop_loss_pct / 100.0)
        target_price = net_debit * (1.0 + target_pct / 100.0)

        # Time stop: 5 DTE before far-expiry leg
        time_stop = datetime.combine(
            far_expiry_date - timedelta(days=5),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        buy_leg = Leg(
            option_type="CE",
            strike=buy_strike.strike,
            expiry=far_expiry_date,
            action="BUY",
            lots=1,
            premium=estimated_far_premium,
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
            expiry=far_expiry_date,
            confidence=0.55,
            metadata={
                "buy_strike": buy_strike.strike,
                "sell_strike": sell_strike.strike,
                "far_expiry": far_expiry_date.isoformat(),
                "near_expiry": chain.expiry.isoformat(),
                "net_debit": net_debit,
                "iv_rank": chain.iv_rank,
                "dte_near": dte,
                "dte_far": far_expiry_dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        # Identify the short (sold) leg
        sell_leg = None
        for leg in position.legs:
            if leg.action == "SELL":
                sell_leg = leg
                break

        if sell_leg is None:
            return False

        # Special exit: if short leg goes ITM, close immediately
        spot = current_chain.underlying_price
        if sell_leg.option_type == "CE" and spot > sell_leg.strike:
            logger.info(
                "diagonal_short_leg_itm",
                position_id=position.position_id,
                spot=spot,
                short_strike=sell_leg.strike,
            )
            return True

        # Calculate current spread value
        buy_leg = None
        for leg in position.legs:
            if leg.action == "BUY":
                buy_leg = leg
                break

        if buy_leg is None:
            return False

        buy_current = None
        sell_current = None
        for s in current_chain.strikes:
            if abs(s.strike - buy_leg.strike) < 0.01:
                buy_current = s
            if abs(s.strike - sell_leg.strike) < 0.01:
                sell_current = s

        if buy_current is None or sell_current is None:
            return False

        current_value = buy_current.call_ltp - sell_current.call_ltp
        entry_debit = buy_leg.premium - sell_leg.premium

        if entry_debit <= 0:
            return False

        # Stop loss
        loss_pct = (entry_debit - current_value) / entry_debit * 100
        if loss_pct >= config.get("stop_loss_pct", 40.0):
            return True

        # Target
        gain_pct = (current_value - entry_debit) / entry_debit * 100
        if gain_pct >= config.get("target_pct", 60.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Margin for diagonal = difference between strikes (defined risk)."""
        atm = self.find_atm_strike(chain, "CE")
        otm = self.find_otm_strike(chain, "CE", steps=1)
        if atm and otm:
            return abs(otm.strike - atm.strike)
        return 0.0
