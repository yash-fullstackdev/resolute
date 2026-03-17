"""SMCOrderBlockStrategy — Smart Money Concepts Order Block entry.

Entry logic:
  1. 5m BOS confirms trend direction (HH+HL = bullish, LH+LL = bearish).
  2. Price returns to unmitigated Order Block matching trend.
  3. 1m upward momentum for bullish OB, downward for bearish OB.
  4. Volume ≥ 1.0x average.
  5. Optional: FVG overlap raises confidence.
  6. Optional: liquidity sweep bonus.

Max 5 fires per day per underlying.
"""

from __future__ import annotations

import math
from datetime import datetime, date as _date, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import atr_wilder, volume_ratio
from .smc_helpers import detect_bos_choch, detect_fvg, find_order_blocks

logger = structlog.get_logger(service="user_worker_pool", module="smc_order_block")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


class SMCOrderBlockStrategy(BaseStrategy):
    """Smart Money Concepts — institutional Order Block retracement entry."""

    name = "smc_order_block"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "ADVANCED"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def __init__(self) -> None:
        self._fires: dict[str, int] = {}
        self._last_date: dict[str, str] = {}

    def evaluate(self, chain, regime, open_positions, config):
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        data_5m: dict = chain.candles_5m
        data_1m: dict = chain.candles_1m
        if not data_5m or "close" not in data_5m:
            return None
        if not data_1m or "close" not in data_1m:
            return None

        ob_length = config.get("ob_length", 6)
        fvg_threshold = config.get("fvg_threshold", 0.0005)
        max_fires = config.get("max_fires_per_day", 5)

        # ── daily fire limit ──────────────────────────────────────────
        key = chain.underlying
        today = _date.today().isoformat()
        if self._last_date.get(key) != today:
            self._last_date[key] = today
            self._fires[key] = 0
        if self._fires.get(key, 0) >= max_fires:
            return None

        # 1. 5m structural analysis
        state = detect_bos_choch(
            data_5m["high"], data_5m["low"], data_5m["close"], ob_length
        )
        if not state or state["trend"] == 0:
            return None

        # 2. Active order blocks
        obs = find_order_blocks(data_5m, state, ob_length, limit=5)
        if not obs:
            return None

        # 3. Is 1m price inside an OB?
        curr_price = data_1m["close"][-1]
        active_ob = None
        for ob in obs:
            if ob["btm"] <= curr_price <= ob["top"]:
                if ob["bull"] == (state["trend"] == 1):
                    active_ob = ob
                    break
        if not active_ob:
            return None

        # 4. 1m momentum confirmation
        if len(data_1m["close"]) < 5:
            return None
        recent = data_1m["close"][-3:]
        if state["trend"] == 1 and recent[-1] <= recent[0]:
            return None
        if state["trend"] == -1 and recent[-1] >= recent[0]:
            return None

        # 5. Volume confirmation
        volumes_1m = data_1m.get("volume", [])
        if not volumes_1m or len(volumes_1m) < 20:
            return None
        vol_rat = volume_ratio(volumes_1m, period=20)
        if vol_rat < 1.0:
            return None

        # 6. ATR regime
        atr_vals = atr_wilder(data_5m["high"], data_5m["low"], data_5m["close"], 14)
        if not atr_vals:
            return None
        atr_pct = atr_vals[-1] / curr_price if curr_price > 0 else 0
        if atr_pct < 0.0004:
            return None

        # 7. FVG overlap bonus
        fvgs = detect_fvg(data_5m["high"], data_5m["low"], data_5m["close"], fvg_threshold)
        fvg_overlap = any(f["btm"] <= active_ob["avg"] <= f["top"] for f in fvgs)
        sweep = state.get("last_sweep", False)

        signal_dir = "BUY" if state["trend"] == 1 else "SELL"
        self._fires[key] = self._fires.get(key, 0) + 1

        # Dynamic confidence
        confidence = 0.75
        if fvg_overlap:
            confidence += 0.10
        if sweep:
            confidence += 0.15
        if vol_rat > 1.3:
            confidence += 0.05
        confidence = min(confidence, 0.95)

        spot = curr_price
        option_type = "CE" if signal_dir == "BUY" else "PE"
        dte = self.get_dte(chain)

        if chain.strikes:
            strike_data = self.find_atm_strike(chain, option_type)
            if strike_data is None:
                return None
            premium = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
            if premium <= 0:
                premium = _estimate_premium(spot, chain.atm_iv, dte)
            strike_val = strike_data.strike
        else:
            strike_val = _atm_strike(spot, chain.underlying)
            premium = _estimate_premium(spot, chain.atm_iv, dte)

        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 100.0)
        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        now = datetime.now(timezone.utc)
        time_stop = now.replace(hour=9, minute=50, second=0, microsecond=0)
        if time_stop <= now:
            time_stop = now + timedelta(hours=2)

        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=chain.underlying,
            segment=config.get("segment", "NSE_INDEX"),
            direction="BULLISH" if signal_dir == "BUY" else "BEARISH",
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=round(confidence, 2),
            metadata={
                "signal_type": signal_dir,
                "ob_top": round(active_ob["top"], 2),
                "ob_btm": round(active_ob["btm"], 2),
                "fvg_overlap": fvg_overlap,
                "sweep": sweep,
                "volume_ratio": round(vol_rat, 2),
            },
        )

    def should_exit(self, position, current_chain, config):
        data_1m = current_chain.candles_1m
        if not data_1m or "close" not in data_1m:
            return False
        curr_price = data_1m["close"][-1]
        return (curr_price <= position.stop_loss_price
                or curr_price >= position.target_price
                or datetime.now(timezone.utc) >= position.time_stop)
