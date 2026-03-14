"""
CoveredCallStrategy -- long futures + sell OTM call.

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is BULL_LOW_VOL or SIDEWAYS (mild bullish to neutral)
  2. IV rank > 35 (want decent premium for sold call)
  3. User holds or is willing to hold long futures
  4. No major upside catalyst expected

Legs:
  Buy/Hold 1 lot futures (long underlying exposure)
  Sell 1 OTM CE (covered_call_otm_strikes OTM, default 2)

Income strategy: collect call premium as yield on long position.
DTE for sold call: 15-30. Roll at 5 DTE.
Stop: futures P&L < -3% -> close both legs.
Target: call decays to 20% of entry -> buy back, sell new.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="covered_call")


class CoveredCallStrategy(BaseStrategy):
    name = "covered_call"
    category = StrategyCategory.SELLING
    min_capital_tier = CapitalTier.PRO
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime is BULL_LOW_VOL or SIDEWAYS
        if regime.value not in {"BULL_LOW_VOL", "SIDEWAYS"}:
            return None

        # Condition 2: IV rank > 35
        iv_rank_min = config.get("iv_rank_min", 35)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: No major upside catalyst
        if hasattr(chain, "next_event_days") and chain.next_event_days is not None:
            if chain.next_event_days <= config.get("event_exclusion_days", 5):
                return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 15-30
        dte = self.get_dte(chain)
        if dte < 15 or dte > 30:
            return None

        # -- Leg construction
        otm_steps = config.get("covered_call_otm_strikes", 2)

        # Futures entry price = current underlying price
        futures_price = chain.underlying_price
        lot_size = config.get("lot_size", 50)

        # Sell OTM call
        sell_strike = self.find_otm_strike(chain, "CE", steps=otm_steps)
        if sell_strike is None:
            return None

        sell_premium = sell_strike.call_ltp
        if sell_premium <= 0:
            return None

        # For covered call, entry_price is the call premium received (credit)
        entry_price = sell_premium

        # Max profit = (strike - futures_entry) + premium per unit
        max_profit_per_unit = (sell_strike.strike - futures_price) + sell_premium
        max_profit = max_profit_per_unit * lot_size

        # Stop: futures down 3%
        futures_stop_loss_pct = config.get("futures_stop_loss_pct", 3.0)
        futures_stop_price = futures_price * (1.0 - futures_stop_loss_pct / 100.0)

        # Profit target: call decays to 20% of entry
        profit_target_pct = config.get("profit_target_pct", 80.0)
        target_price = sell_premium * (1.0 - profit_target_pct / 100.0)

        # Max loss estimate: futures 5% down - premium
        estimated_max_loss = futures_price * lot_size * 0.05 - sell_premium * lot_size

        # Time stop: roll at 5 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=5),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        # Represent futures as a synthetic "CE" leg at ATM with BUY action
        # This is a representation convention for the position tracker
        futures_leg = Leg(
            option_type="CE",
            strike=futures_price,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=futures_price,  # futures entry price
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
            legs=[futures_leg, sell_leg],
            entry_price=entry_price,
            stop_loss_pct=futures_stop_loss_pct,
            stop_loss_price=futures_stop_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=estimated_max_loss,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "futures_price": futures_price,
                "sell_strike": sell_strike.strike,
                "sell_premium": sell_premium,
                "max_profit": max_profit,
                "futures_stop_price": futures_stop_price,
                "lot_size": lot_size,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        spot = current_chain.underlying_price

        # Find the futures leg (BUY) and sold call leg (SELL)
        futures_leg = None
        sell_leg = None
        for leg in position.legs:
            if leg.action == "BUY":
                futures_leg = leg
            elif leg.action == "SELL":
                sell_leg = leg

        if futures_leg is None or sell_leg is None:
            return False

        futures_entry = futures_leg.premium  # stored as futures entry price

        # Exit 1: Futures P&L < -3%
        futures_stop_loss_pct = config.get("futures_stop_loss_pct", 3.0)
        futures_pnl_pct = (spot - futures_entry) / futures_entry * 100
        if futures_pnl_pct <= -futures_stop_loss_pct:
            logger.info(
                "covered_call_futures_stop",
                position_id=position.position_id,
                spot=spot,
                futures_entry=futures_entry,
                pnl_pct=round(futures_pnl_pct, 2),
            )
            return True

        # Exit 2: Time stop -- roll at 5 DTE
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        # Exit 3: Call decayed to 20% of entry -- buy back and sell new
        sell_current = None
        for s in current_chain.strikes:
            if abs(s.strike - sell_leg.strike) < 0.01:
                sell_current = s
                break

        if sell_current is not None:
            current_premium = sell_current.call_ltp
            entry_premium = sell_leg.premium
            if entry_premium > 0:
                decay_pct = (entry_premium - current_premium) / entry_premium * 100
                profit_target_pct = config.get("profit_target_pct", 80.0)
                if decay_pct >= profit_target_pct:
                    return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Covered call margin = futures margin.

        Approximate: underlying_price x lot_size x 0.12 (futures margin).
        The short call is covered by the long futures.
        """
        underlying_price = chain.underlying_price
        lot_size = config.get("lot_size", 50)
        return underlying_price * lot_size * 0.12
