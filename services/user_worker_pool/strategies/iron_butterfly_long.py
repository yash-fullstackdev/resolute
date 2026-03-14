"""
IronButterflyLongStrategy -- long straddle + short wings (defined risk).

HYBRID category (GROWTH tier, 50k+).

Entry conditions:
  1. Regime is PRE_EVENT or HIGH_VOL_SPIKE (expecting big move)
  2. IV rank > 40 (selling wings is profitable at elevated IV)
  3. IV rank < 80 (not selling at peak)
  4. No existing iron_butterfly_long on same underlying

Legs (all same expiry):
  Buy 1 ATM CE + Buy 1 ATM PE (long straddle core)
  Sell 1 OTM CE + Sell 1 OTM PE (wing_width strikes OTM)

Max profit = unlimited beyond wings minus net premium
Max loss = wing_width x lot_size - net credit received (debit strategy)
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="iron_butterfly_long")


class IronButterflyLongStrategy(BaseStrategy):
    name = "iron_butterfly_long"
    category = StrategyCategory.HYBRID
    min_capital_tier = CapitalTier.GROWTH
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime must be PRE_EVENT or HIGH_VOL_SPIKE
        if regime.value not in {"PRE_EVENT", "HIGH_VOL_SPIKE"}:
            return None

        # Condition 2: IV rank > 40
        iv_rank_min = config.get("iv_rank_min", 40)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: IV rank < 80
        iv_rank_max = config.get("iv_rank_max", 80)
        if chain.iv_rank > iv_rank_max:
            logger.debug(
                "iron_butterfly_skip_extreme_iv",
                underlying=underlying,
                iv_rank=chain.iv_rank,
            )
            return None

        # Condition 4: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 7-25 DTE
        dte = self.get_dte(chain)
        if dte < 7 or dte > 25:
            return None

        # -- Leg construction
        wing_width = config.get("wing_width", 3)

        atm_ce = self.find_atm_strike(chain, "CE")
        atm_pe = self.find_atm_strike(chain, "PE")
        otm_ce = self.find_otm_strike(chain, "CE", steps=wing_width)
        otm_pe = self.find_otm_strike(chain, "PE", steps=wing_width)

        if any(s is None for s in (atm_ce, atm_pe, otm_ce, otm_pe)):
            return None

        buy_ce_premium = atm_ce.call_ltp
        buy_pe_premium = atm_pe.put_ltp
        sell_ce_premium = otm_ce.call_ltp
        sell_pe_premium = otm_pe.put_ltp

        if any(p <= 0 for p in (buy_ce_premium, buy_pe_premium)):
            return None

        # Net debit = cost of straddle - credit from wings
        straddle_cost = buy_ce_premium + buy_pe_premium
        wing_credit = sell_ce_premium + sell_pe_premium
        net_debit = straddle_cost - wing_credit

        if net_debit <= 0:
            # Should be a debit; if credit, something unusual
            return None

        # Max loss = net debit (wings define the risk)
        # Stop-loss and target
        stop_loss_pct = config.get("stop_loss_pct", 35.0)
        target_pct = config.get("target_pct", 100.0)

        stop_loss_price = net_debit * (1.0 - stop_loss_pct / 100.0)
        target_price = net_debit * (1.0 + target_pct / 100.0)

        # Time stop: event day 14:30 IST (09:00 UTC)
        time_stop = datetime.combine(
            chain.expiry,
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        buy_ce_leg = Leg(
            option_type="CE",
            strike=atm_ce.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_ce_premium,
        )
        buy_pe_leg = Leg(
            option_type="PE",
            strike=atm_pe.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=buy_pe_premium,
        )
        sell_ce_leg = Leg(
            option_type="CE",
            strike=otm_ce.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_ce_premium,
        )
        sell_pe_leg = Leg(
            option_type="PE",
            strike=otm_pe.strike,
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
            legs=[buy_ce_leg, buy_pe_leg, sell_ce_leg, sell_pe_leg],
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
                "atm_strike": atm_ce.strike,
                "sell_ce_strike": otm_ce.strike,
                "sell_pe_strike": otm_pe.strike,
                "straddle_cost": straddle_cost,
                "wing_credit": wing_credit,
                "net_debit": net_debit,
                "wing_width": wing_width,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 4:
            return False

        # Calculate current value of all 4 legs
        total_value = 0.0
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
            if leg.action == "BUY":
                total_value += current_premium
            else:
                total_value -= current_premium

        # Entry was a net debit
        entry_debit = sum(
            l.premium if l.action == "BUY" else -l.premium
            for l in position.legs
        )

        if entry_debit <= 0:
            return False

        # Stop loss: premium loss exceeds threshold
        loss_pct = (entry_debit - total_value) / entry_debit * 100
        if loss_pct >= config.get("stop_loss_pct", 35.0):
            return True

        # Target: position value gained
        gain_pct = (total_value - entry_debit) / entry_debit * 100
        if gain_pct >= config.get("target_pct", 100.0):
            return True

        # Time stop
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Margin for iron butterfly = wing width (defined risk spread)."""
        wing_width = config.get("wing_width", 3)
        atm = self.find_atm_strike(chain, "CE")
        otm = self.find_otm_strike(chain, "CE", steps=wing_width)
        if atm and otm:
            return abs(otm.strike - atm.strike)
        return 0.0
