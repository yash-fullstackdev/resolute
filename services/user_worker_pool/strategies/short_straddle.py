"""
ShortStraddleStrategy -- sell ATM CE + ATM PE.

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is SIDEWAYS (range-bound, no event)
  2. IV rank > 60 (selling expensive options)
  3. VIX > 16 but < 25 (elevated but not panic)
  4. No major event within event_exclusion_days (default 3)
  5. No existing short_straddle on same underlying
  6. Sufficient margin

Legs (same expiry):
  Sell 1 ATM CE + Sell 1 ATM PE

Max profit = total credit received
Max loss = UNLIMITED
DTE: 15-25 DTE (sweet spot for theta)
Stop: 1.5x credit. Target: 50% decay.
Adjustment trigger at 1.5% move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="short_straddle")


class ShortStraddleStrategy(BaseStrategy):
    name = "short_straddle"
    category = StrategyCategory.SELLING
    min_capital_tier = CapitalTier.PRO
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime must be SIDEWAYS
        if regime.value != "SIDEWAYS":
            return None

        # Condition 2: IV rank > 60
        iv_rank_min = config.get("iv_rank_min", 60)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: VIX between 16 and 25
        vix_min = config.get("vix_min", 16)
        vix_max = config.get("vix_max", 25)
        if hasattr(chain, "vix") and chain.vix is not None:
            if chain.vix < vix_min or chain.vix >= vix_max:
                logger.debug(
                    "short_straddle_skip_vix",
                    underlying=underlying,
                    vix=chain.vix,
                )
                return None

        # Condition 4: No major event within exclusion days
        event_exclusion_days = config.get("event_exclusion_days", 3)
        if hasattr(chain, "next_event_days") and chain.next_event_days is not None:
            if chain.next_event_days <= event_exclusion_days:
                logger.debug(
                    "short_straddle_skip_event",
                    underlying=underlying,
                    next_event_days=chain.next_event_days,
                )
                return None

        # Condition 5: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 15-25 DTE (never sell weekly expiry straddles)
        dte = self.get_dte(chain)
        if dte < 15 or dte > 25:
            return None

        # -- Leg construction
        atm_ce = self.find_atm_strike(chain, "CE")
        atm_pe = self.find_atm_strike(chain, "PE")

        if atm_ce is None or atm_pe is None:
            return None

        sell_ce_premium = atm_ce.call_ltp
        sell_pe_premium = atm_pe.put_ltp

        if sell_ce_premium <= 0 or sell_pe_premium <= 0:
            return None

        total_credit = sell_ce_premium + sell_pe_premium

        # Stop-loss: 1.5x credit
        credit_loss_multiple = config.get("credit_loss_multiple", 1.5)
        stop_loss_price = total_credit * (1.0 + credit_loss_multiple)

        # Profit target: 50% of credit
        profit_target_pct = config.get("profit_target_pct", 50.0)
        target_price = total_credit * (1.0 - profit_target_pct / 100.0)

        # Time stop: 3 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=3),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        # Max loss is unlimited, but for sizing we estimate a practical max
        adjustment_trigger_pct = config.get("adjustment_trigger_pct", 1.5)
        lot_size = config.get("lot_size", 50)
        estimated_max_loss = chain.underlying_price * lot_size * 0.05  # 5% move

        sell_ce_leg = Leg(
            option_type="CE",
            strike=atm_ce.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_ce_premium,
        )
        sell_pe_leg = Leg(
            option_type="PE",
            strike=atm_pe.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_pe_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="NEUTRAL",
            legs=[sell_ce_leg, sell_pe_leg],
            entry_price=total_credit,
            stop_loss_pct=credit_loss_multiple * 100,
            stop_loss_price=stop_loss_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=estimated_max_loss,
            expiry=chain.expiry,
            confidence=0.65,
            metadata={
                "atm_strike": atm_ce.strike,
                "total_credit": total_credit,
                "ce_premium": sell_ce_premium,
                "pe_premium": sell_pe_premium,
                "credit_loss_multiple": credit_loss_multiple,
                "adjustment_trigger_pct": adjustment_trigger_pct,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        # Calculate current cost to close (value of sold options)
        total_current = 0.0
        for leg in position.legs:
            strike_data = None
            for s in current_chain.strikes:
                if abs(s.strike - leg.strike) < 0.01:
                    strike_data = s
                    break
            if strike_data is None:
                return False

            current_premium = (
                strike_data.call_ltp if leg.option_type == "CE"
                else strike_data.put_ltp
            )
            total_current += current_premium

        entry_credit = position.entry_cost_inr
        if entry_credit <= 0:
            return False

        # Exit 1: Loss exceeds 1.5x initial credit
        credit_loss_multiple = config.get("credit_loss_multiple", 1.5)
        loss = total_current - entry_credit
        if loss > 0 and loss / entry_credit >= credit_loss_multiple:
            return True

        # Exit 2: Time stop (3 DTE)
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        # Exit 3: Profit target -- current value < 50% of initial credit
        profit_target_pct = config.get("profit_target_pct", 50.0)
        decay_pct = (entry_credit - total_current) / entry_credit * 100
        if decay_pct >= profit_target_pct:
            return True

        # Exit 4: VIX spike above 28
        if hasattr(current_chain, "vix") and current_chain.vix is not None:
            if current_chain.vix > 28:
                logger.info(
                    "short_straddle_vix_panic",
                    position_id=position.position_id,
                    vix=current_chain.vix,
                )
                return True

        # Exit 5: Underlying moved > 2.5% from entry
        if position.legs:
            entry_strike = position.legs[0].strike  # ATM at entry
            spot = current_chain.underlying_price
            move_pct = abs(spot - entry_strike) / entry_strike * 100
            if move_pct > config.get("max_underlying_move_pct", 2.5):
                return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Approximate margin: underlying_price x lot_size x 0.18."""
        underlying_price = chain.underlying_price
        lot_size = config.get("lot_size", 50)
        return underlying_price * lot_size * 0.18
