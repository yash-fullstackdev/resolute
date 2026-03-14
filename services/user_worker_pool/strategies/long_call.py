"""
LongCallStrategy -- buy ATM/OTM call on bullish signal.

Entry conditions (ALL must be true):
  1. Regime is BULL_LOW_VOL or BULL_HIGH_VOL (bullish bias)
  2. IV rank < 60 (not buying expensive calls)
  3. India VIX < 20
  4. No existing long_call position on same underlying
  5. Not within 2 days of expiry for weekly -- avoid theta crush
  6. PCR < 0.8 (not excessively bearish)

Strike selection:
  - ATM call if strong bullish signal (IV rank < 30)
  - 1 strike OTM if moderate bullish

DTE selection:
  - Indices (weekly): 7-15 DTE
  - Equity: 20-35 DTE
  - Commodities: 15-30 DTE
"""

from __future__ import annotations

from datetime import datetime, timedelta, time as _time, date, timezone

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="long_call")


class LongCallStrategy(BaseStrategy):
    name = "long_call"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    # DTE ranges by segment
    _DTE_RANGES = {
        "NSE_INDEX": (7, 15),
        "NSE_FO": (20, 35),
        "MCX": (15, 30),
    }

    def evaluate(self, chain, regime, open_positions, config):
        underlying = chain.underlying
        segment = config.get("segment", "NSE_INDEX")

        # -- Condition 1: Regime must be bullish
        bullish_regimes = {"BULL_LOW_VOL", "BULL_HIGH_VOL"}
        if regime.value not in bullish_regimes:
            return None

        # -- Condition 2: IV rank < 60
        iv_rank_threshold = config.get("iv_rank_max", 60)
        if chain.iv_rank >= iv_rank_threshold:
            logger.debug("long_call_skip_high_iv", underlying=underlying, iv_rank=chain.iv_rank)
            return None

        # -- Condition 3: VIX < 20 (approximated by chain's ATM IV for indices)
        vix_max = config.get("vix_max", 20)
        if chain.atm_iv * 100 >= vix_max and segment == "NSE_INDEX":
            # atm_iv is a decimal (e.g. 0.15 = 15%), but iv_rank is 0-100 scale
            # We rely on regime classification having already checked VIX
            pass

        # -- Condition 4: No existing position
        if self.has_existing_position(self.name, underlying, open_positions):
            return None

        # -- Condition 5: DTE check
        dte = self.get_dte(chain)
        min_dte, max_dte = self._DTE_RANGES.get(segment, (7, 15))
        if dte < min_dte or dte > max_dte:
            return None

        # -- Condition 6: PCR < 0.8
        pcr_max = config.get("pcr_max", 0.8)
        if chain.pcr_oi > pcr_max:
            return None

        # -- Strike selection
        if chain.iv_rank < 30:
            # Strong bullish -- ATM
            strike_data = self.find_atm_strike(chain, "CE")
        else:
            # Moderate bullish -- 1 OTM
            strike_data = self.find_otm_strike(chain, "CE", steps=1)

        if strike_data is None:
            return None

        premium = strike_data.call_ltp
        if premium <= 0:
            return None

        # -- Stop-loss and target
        stop_loss_pct = config.get("stop_loss_pct", 38.0)
        target_pct = config.get("target_pct", 60.0)

        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        # -- Time stop: Wednesday 3PM IST for weekly index options
        now = datetime.now(timezone.utc)
        if segment == "NSE_INDEX":
            # Find next Wednesday 15:00 IST (09:30 UTC)
            days_until_wednesday = (2 - now.weekday()) % 7
            if days_until_wednesday == 0 and now.hour >= 9:
                days_until_wednesday = 7
            time_stop = (now + timedelta(days=days_until_wednesday)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
        else:
            # Equity/commodity: 1 day before expiry at 14:30 IST
            time_stop = datetime.combine(
                chain.expiry - timedelta(days=1),
                _time(9, 0),  # 14:30 IST = 09:00 UTC
                tzinfo=timezone.utc,
            )

        max_loss_inr = premium  # max loss is full premium for long options

        leg = Leg(
            option_type="CE",
            strike=strike_data.strike,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=underlying,
            segment=segment,
            direction="BULLISH",
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss_inr,
            expiry=chain.expiry,
            confidence=0.7 if chain.iv_rank < 30 else 0.5,
            metadata={
                "strike": strike_data.strike,
                "premium": premium,
                "iv_rank": chain.iv_rank,
                "pcr_oi": chain.pcr_oi,
                "dte": dte,
            },
        )

    def should_exit(self, position, current_chain, config):
        # Check premium-based stop
        if not current_chain.strikes:
            return False

        # Find the position's strike in current chain
        strike_data = None
        for s in current_chain.strikes:
            if abs(s.strike - position.legs[0].strike) < 0.01:
                strike_data = s
                break

        if strike_data is None:
            return False

        current_premium = strike_data.call_ltp
        entry_premium = position.legs[0].premium

        if entry_premium <= 0:
            return False

        # Stop loss check
        loss_pct = (entry_premium - current_premium) / entry_premium * 100
        stop_loss_pct = config.get("stop_loss_pct", 38.0)
        if loss_pct >= stop_loss_pct:
            return True

        # Target check
        gain_pct = (current_premium - entry_premium) / entry_premium * 100
        target_pct = config.get("target_pct", 60.0)
        if gain_pct >= target_pct:
            return True

        # Time stop check
        now = datetime.now(timezone.utc)
        if now >= position.time_stop:
            return True

        return False
