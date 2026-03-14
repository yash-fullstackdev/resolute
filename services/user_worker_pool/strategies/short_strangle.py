"""
ShortStrangleStrategy -- sell OTM CE + OTM PE.

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is SIDEWAYS or BULL_LOW_VOL
  2. IV rank > 50 (selling elevated IV)
  3. VIX > 14 but < 22
  4. No major event within 3 days
  5. Sufficient margin

Legs (same expiry):
  Sell 1 OTM CE (strangle_otm_strikes OTM, default 3)
  Sell 1 OTM PE (same distance OTM)

Wider breakevens than straddle = higher win rate, lower premium.
DTE: 20-30. Stop: 2x credit. Target: 50%.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="short_strangle")


class ShortStrangleStrategy(BaseStrategy):
    name = "short_strangle"
    category = StrategyCategory.SELLING
    min_capital_tier = CapitalTier.PRO
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime is SIDEWAYS or BULL_LOW_VOL
        if regime.value not in {"SIDEWAYS", "BULL_LOW_VOL"}:
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

        # Condition 4: No major event within 3 days
        event_exclusion_days = config.get("event_exclusion_days", 3)
        if hasattr(chain, "next_event_days") and chain.next_event_days is not None:
            if chain.next_event_days <= event_exclusion_days:
                return None

        # Condition 5: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 20-30 DTE
        dte = self.get_dte(chain)
        if dte < 20 or dte > 30:
            return None

        # -- Leg construction
        otm_steps = config.get("strangle_otm_strikes", 3)

        otm_ce = self.find_otm_strike(chain, "CE", steps=otm_steps)
        otm_pe = self.find_otm_strike(chain, "PE", steps=otm_steps)

        if otm_ce is None or otm_pe is None:
            return None

        sell_ce_premium = otm_ce.call_ltp
        sell_pe_premium = otm_pe.put_ltp

        if sell_ce_premium <= 0 or sell_pe_premium <= 0:
            return None

        total_credit = sell_ce_premium + sell_pe_premium

        # Stop-loss: 2x credit
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        stop_loss_price = total_credit * (1.0 + credit_loss_multiple)

        # Profit target: 50% of credit
        profit_target_pct = config.get("profit_target_pct", 50.0)
        target_price = total_credit * (1.0 - profit_target_pct / 100.0)

        # Time stop: 5 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=5),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        lot_size = config.get("lot_size", 50)
        estimated_max_loss = chain.underlying_price * lot_size * 0.05

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
            legs=[sell_ce_leg, sell_pe_leg],
            entry_price=total_credit,
            stop_loss_pct=credit_loss_multiple * 100,
            stop_loss_price=stop_loss_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=estimated_max_loss,
            expiry=chain.expiry,
            confidence=0.6,
            metadata={
                "ce_strike": otm_ce.strike,
                "pe_strike": otm_pe.strike,
                "total_credit": total_credit,
                "ce_premium": sell_ce_premium,
                "pe_premium": sell_pe_premium,
                "credit_loss_multiple": credit_loss_multiple,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 2:
            return False

        # Calculate current cost to close
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

        # Exit 1: Loss exceeds 2x credit
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        loss = total_current - entry_credit
        if loss > 0 and loss / entry_credit >= credit_loss_multiple:
            return True

        # Exit 2: Time stop (5 DTE)
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        # Exit 3: Profit target -- 50% decay
        profit_target_pct = config.get("profit_target_pct", 50.0)
        decay_pct = (entry_credit - total_current) / entry_credit * 100
        if decay_pct >= profit_target_pct:
            return True

        # Exit 4: VIX spike above 28
        if hasattr(current_chain, "vix") and current_chain.vix is not None:
            if current_chain.vix > 28:
                return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Strangle margin ~ underlying_price x lot_size x 0.16."""
        underlying_price = chain.underlying_price
        lot_size = config.get("lot_size", 50)
        return underlying_price * lot_size * 0.16
