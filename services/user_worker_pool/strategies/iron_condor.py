"""
IronCondorStrategy -- call credit spread + put credit spread combined.

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is SIDEWAYS (strong range-bound conviction)
  2. IV rank > 50 (selling elevated premium on both sides)
  3. VIX between 14-22
  4. No major event within 5 days
  5. Historical range: underlying stayed within +/-2% for 5+ sessions

Legs (all same expiry):
  Bull put spread: Sell OTM PE + Buy further OTM PE
  Bear call spread: Sell OTM CE + Buy further OTM CE

condor_wing_width: distance from ATM for short strikes (default 3)
condor_spread_width: distance between short and long strikes (default 2)

DTE: 20-30. Stop: breach trigger (close tested side). Target: 50% credit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="iron_condor")


class IronCondorStrategy(BaseStrategy):
    name = "iron_condor"
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

        # Condition 2: IV rank > 50
        iv_rank_min = config.get("iv_rank_min", 50)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: VIX between 14 and 22
        vix_min = config.get("vix_min", 14)
        vix_max = config.get("vix_max", 22)
        if hasattr(chain, "vix") and chain.vix is not None:
            if chain.vix < vix_min or chain.vix >= vix_max:
                return None

        # Condition 4: No major event within 5 days
        event_exclusion_days = config.get("event_exclusion_days", 5)
        if hasattr(chain, "next_event_days") and chain.next_event_days is not None:
            if chain.next_event_days <= event_exclusion_days:
                return None

        # Condition 5: Historical range check
        if hasattr(chain, "range_5d_pct") and chain.range_5d_pct is not None:
            if chain.range_5d_pct > config.get("max_range_5d_pct", 2.0):
                return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 20-30
        dte = self.get_dte(chain)
        if dte < 20 or dte > 30:
            return None

        # -- Leg construction
        wing_width = config.get("condor_wing_width", 3)
        spread_width = config.get("condor_spread_width", 2)

        # Call credit spread (bear call)
        sell_ce = self.find_otm_strike(chain, "CE", steps=wing_width)
        buy_ce = self.find_otm_strike(chain, "CE", steps=wing_width + spread_width)

        # Put credit spread (bull put)
        sell_pe = self.find_otm_strike(chain, "PE", steps=wing_width)
        buy_pe = self.find_otm_strike(chain, "PE", steps=wing_width + spread_width)

        if any(s is None for s in (sell_ce, buy_ce, sell_pe, buy_pe)):
            return None

        if buy_ce.strike <= sell_ce.strike or buy_pe.strike >= sell_pe.strike:
            return None

        sell_ce_premium = sell_ce.call_ltp
        buy_ce_premium = buy_ce.call_ltp
        sell_pe_premium = sell_pe.put_ltp
        buy_pe_premium = buy_pe.put_ltp

        if any(p <= 0 for p in (sell_ce_premium, sell_pe_premium)):
            return None

        call_credit = sell_ce_premium - buy_ce_premium
        put_credit = sell_pe_premium - buy_pe_premium
        total_credit = call_credit + put_credit

        if total_credit <= 0:
            return None

        # Max loss = wider spread width - total credit
        call_spread_width = buy_ce.strike - sell_ce.strike
        put_spread_width = sell_pe.strike - buy_pe.strike
        wider_spread = max(call_spread_width, put_spread_width)
        max_loss = wider_spread - total_credit

        if max_loss <= 0:
            return None

        # Profit target: 50% of total credit
        profit_target_pct = config.get("profit_target_pct", 50.0)
        target_price = total_credit * (1.0 - profit_target_pct / 100.0)

        # Stop: breach trigger based
        stop_loss_price = total_credit * 3.0  # fallback numeric stop

        # Time stop: 5 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=5),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        sell_ce_leg = Leg(
            option_type="CE",
            strike=sell_ce.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_ce_premium,
        )
        buy_ce_leg = Leg(
            option_type="CE",
            strike=buy_ce.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_ce_premium,
        )
        sell_pe_leg = Leg(
            option_type="PE",
            strike=sell_pe.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_pe_premium,
        )
        buy_pe_leg = Leg(
            option_type="PE",
            strike=buy_pe.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_pe_premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="NEUTRAL",
            legs=[sell_ce_leg, buy_ce_leg, sell_pe_leg, buy_pe_leg],
            entry_price=total_credit,
            stop_loss_pct=0.0,  # breach-based, not percentage
            stop_loss_price=stop_loss_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "sell_ce_strike": sell_ce.strike,
                "buy_ce_strike": buy_ce.strike,
                "sell_pe_strike": sell_pe.strike,
                "buy_pe_strike": buy_pe.strike,
                "call_credit": call_credit,
                "put_credit": put_credit,
                "total_credit": total_credit,
                "max_loss": max_loss,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 4:
            return False

        spot = current_chain.underlying_price

        # Identify short strikes
        short_ce_strike = None
        short_pe_strike = None
        for leg in position.legs:
            if leg.action == "SELL" and leg.option_type == "CE":
                short_ce_strike = leg.strike
            elif leg.action == "SELL" and leg.option_type == "PE":
                short_pe_strike = leg.strike

        if short_ce_strike is None or short_pe_strike is None:
            return False

        # Exit 1: Breach trigger -- underlying approaches within 0.5% of short strike
        breach_threshold_pct = config.get("breach_threshold_pct", 0.5)
        ce_distance_pct = (short_ce_strike - spot) / spot * 100
        pe_distance_pct = (spot - short_pe_strike) / spot * 100

        if ce_distance_pct <= breach_threshold_pct:
            logger.info(
                "iron_condor_ce_breach",
                position_id=position.position_id,
                spot=spot,
                short_ce=short_ce_strike,
                distance_pct=round(ce_distance_pct, 2),
            )
            return True

        if pe_distance_pct <= breach_threshold_pct:
            logger.info(
                "iron_condor_pe_breach",
                position_id=position.position_id,
                spot=spot,
                short_pe=short_pe_strike,
                distance_pct=round(pe_distance_pct, 2),
            )
            return True

        # Calculate current cost to close all legs
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
            if leg.action == "SELL":
                total_current += current_premium
            else:
                total_current -= current_premium

        entry_credit = position.entry_cost_inr
        if entry_credit <= 0:
            return False

        # Exit 2: Profit target -- 50% credit
        profit_target_pct = config.get("profit_target_pct", 50.0)
        decay_pct = (entry_credit - total_current) / entry_credit * 100
        if decay_pct >= profit_target_pct:
            return True

        # Exit 3: VIX spike > 25
        if hasattr(current_chain, "vix") and current_chain.vix is not None:
            if current_chain.vix > 25:
                return True

        # Exit 4: Time stop (5 DTE)
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Iron condor margin = wider spread width x lot_size."""
        spread_width = config.get("condor_spread_width", 2)
        if len(chain.strikes) > 1:
            strike_gap = chain.strikes[1].strike - chain.strikes[0].strike
        else:
            strike_gap = 50
        lot_size = config.get("lot_size", 50)
        return spread_width * strike_gap * lot_size
