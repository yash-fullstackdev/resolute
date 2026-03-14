"""
JadeLizardStrategy -- short put + short call spread (no upside risk).

SELLING category (PRO tier, 2L+).

Entry conditions:
  1. Regime is BULL_LOW_VOL or BULL_MEDIUM_VOL (bullish bias)
  2. IV rank > 45
  3. IV skew: put IV > call IV (typical positive skew)

Legs (same expiry):
  Sell 1 OTM PE (2-3 strikes OTM)
  Sell 1 OTM CE (1-2 strikes OTM)
  Buy 1 further OTM CE (2 strikes above sold CE)

Key: NO upside risk if total credit > call spread width.
Downside risk = short put strike x lot_size - total credit.
DTE: 15-25. Stop: 2x credit on put side. Target: 50%.
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="jade_lizard")


class JadeLizardStrategy(BaseStrategy):
    name = "jade_lizard"
    category = StrategyCategory.SELLING
    min_capital_tier = CapitalTier.PRO
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = True

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # Condition 1: Regime is BULL_LOW_VOL or BULL_MEDIUM_VOL
        if regime.value not in {"BULL_LOW_VOL", "BULL_MEDIUM_VOL"}:
            return None

        # Condition 2: IV rank > 45
        iv_rank_min = config.get("iv_rank_min", 45)
        if chain.iv_rank < iv_rank_min:
            return None

        # Condition 3: IV skew check -- put IV > call IV
        if hasattr(chain, "put_iv") and hasattr(chain, "call_iv"):
            if chain.put_iv is not None and chain.call_iv is not None:
                if chain.put_iv <= chain.call_iv:
                    logger.debug(
                        "jade_lizard_skip_no_skew",
                        underlying=underlying,
                        put_iv=chain.put_iv,
                        call_iv=chain.call_iv,
                    )
                    return None

        # No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # DTE check: 15-25
        dte = self.get_dte(chain)
        if dte < 15 or dte > 25:
            return None

        # -- Leg construction
        put_otm_steps = config.get("jade_put_otm_strikes", 2)
        call_sell_otm_steps = config.get("jade_call_sell_otm_strikes", 1)
        call_buy_offset = config.get("jade_call_buy_offset", 2)

        # Short put
        sell_pe = self.find_otm_strike(chain, "PE", steps=put_otm_steps)
        # Short call
        sell_ce = self.find_otm_strike(chain, "CE", steps=call_sell_otm_steps)
        # Long call (cap upside)
        buy_ce = self.find_otm_strike(chain, "CE", steps=call_sell_otm_steps + call_buy_offset)

        if any(s is None for s in (sell_pe, sell_ce, buy_ce)):
            return None

        if buy_ce.strike <= sell_ce.strike:
            return None

        sell_pe_premium = sell_pe.put_ltp
        sell_ce_premium = sell_ce.call_ltp
        buy_ce_premium = buy_ce.call_ltp

        if sell_pe_premium <= 0 or sell_ce_premium <= 0:
            return None

        # Total credit = put premium + call spread credit
        call_spread_credit = sell_ce_premium - buy_ce_premium
        total_credit = sell_pe_premium + call_spread_credit

        if total_credit <= 0:
            return None

        # Check for no upside risk: total credit >= call spread width
        call_spread_width = buy_ce.strike - sell_ce.strike
        has_no_upside_risk = total_credit >= call_spread_width

        # Max loss on downside = distance to zero from short put - credit (practical: short put strike - credit)
        lot_size = config.get("lot_size", 50)
        max_loss = (sell_pe.strike * lot_size) - (total_credit * lot_size)

        # Stop: 2x credit on put side
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        stop_loss_price = total_credit * (1.0 + credit_loss_multiple)

        # Target: 50%
        profit_target_pct = config.get("profit_target_pct", 50.0)
        target_price = total_credit * (1.0 - profit_target_pct / 100.0)

        # Time stop: 3 DTE
        time_stop = datetime.combine(
            chain.expiry - timedelta(days=3),
            _time(9, 0),
            tzinfo=timezone.utc,
        )

        sell_pe_leg = Leg(
            option_type="PE",
            strike=sell_pe.strike,
            expiry=chain.expiry,
            action="SELL",
            lots=1,
            premium=sell_pe_premium,
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

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="BULLISH",
            legs=[sell_pe_leg, sell_ce_leg, buy_ce_leg],
            entry_price=total_credit,
            stop_loss_pct=credit_loss_multiple * 100,
            stop_loss_price=stop_loss_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss,
            expiry=chain.expiry,
            confidence=0.6 if has_no_upside_risk else 0.5,
            metadata={
                "sell_pe_strike": sell_pe.strike,
                "sell_ce_strike": sell_ce.strike,
                "buy_ce_strike": buy_ce.strike,
                "total_credit": total_credit,
                "call_spread_credit": call_spread_credit,
                "put_premium": sell_pe_premium,
                "call_spread_width": call_spread_width,
                "no_upside_risk": has_no_upside_risk,
                "iv_rank": chain.iv_rank,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.strikes or len(position.legs) < 3:
            return False

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

        # Exit 1: Credit loss multiple (2x on put side)
        credit_loss_multiple = config.get("credit_loss_multiple", 2.0)
        loss = total_current - entry_credit
        if loss > 0 and loss / entry_credit >= credit_loss_multiple:
            return True

        # Exit 2: Time stop (3 DTE)
        if datetime.now(timezone.utc) >= position.time_stop:
            return True

        # Exit 3: Profit target -- 50% decay
        profit_target_pct = config.get("profit_target_pct", 50.0)
        decay_pct = (entry_credit - total_current) / entry_credit * 100
        if decay_pct >= profit_target_pct:
            return True

        return False

    def margin_required_per_lot(self, chain, config):
        """Jade lizard margin = short put margin (naked) since call side is spread.

        Approximate: underlying_price x lot_size x 0.15.
        """
        underlying_price = chain.underlying_price
        lot_size = config.get("lot_size", 50)
        return underlying_price * lot_size * 0.15
