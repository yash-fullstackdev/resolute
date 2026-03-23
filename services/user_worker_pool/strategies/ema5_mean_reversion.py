"""EMA5 Mean Reversion — counter-trend option buying on 5 EMA exhaustion.

When price extends far from the 5-period EMA, a snap-back to the mean is
statistically likely.  Fear (selling) is faster than greed (buying), so:

  - PE signal  →  5m chart:   candle's LOW  > 5 EMA  (price floating above mean)
  - CE signal  → 15m chart:   candle's HIGH < 5 EMA  (price floating below mean)

Alert candle detected → entry on breach of its extremum (low for PE, high for CE).

Risk management:
  - SL   = high of alert candle (PE) | low of alert candle (CE)
  - Target = 1:3 RR from entry
  - Circuit breaker: 3 consecutive SL hits → halt rest of day
  - Avoid if India VIX < 12 (no EMA extension) or > 35 (too erratic)
  - Alert candle must be ≥ 0.2% away from 5 EMA (sufficient extension)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

logger = structlog.get_logger(service="user_worker_pool", module="ema5_mean_reversion")

IST = timezone(timedelta(hours=5, minutes=30))


def _compute_ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Compute EMA for an array of closes.  Returns same-length array (nan for warm-up)."""
    n = len(closes)
    ema = np.full(n, np.nan)
    if n < period:
        return ema
    k = 2.0 / (period + 1)
    ema[period - 1] = np.mean(closes[:period])
    for i in range(period, n):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _itm_strike(spot: float, underlying: str, option_type: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    atm = round(spot / interval) * interval
    if option_type == "CE":
        return atm - interval
    return atm + interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _find_alert_candle(
    candles: dict,
    ema_period: int,
    side: str,              # "PE" (price above EMA) | "CE" (price below EMA)
    min_dist_pct: float,
) -> tuple[float, float, float] | None:
    """Find the most recent valid alert candle.

    For PE: alert candle low > EMA  (price floating above → expect drop)
    For CE: alert candle high < EMA (price floating below → expect rise)

    Returns (trigger_price, sl_price, ema_value) or None.
    The returned candle is the PREVIOUS closed candle (index -2);
    -1 is the current forming bar which we don't act on until it closes.
    """
    closes = candles.get("close")
    highs  = candles.get("high")
    lows   = candles.get("low")
    if closes is None or len(closes) < ema_period + 3:
        return None

    ema = _compute_ema(closes, ema_period)

    # Use second-to-last closed candle as alert candidate
    idx = len(closes) - 2
    if idx < ema_period:
        return None

    ema_val = ema[idx]
    if np.isnan(ema_val):
        return None

    hi  = float(highs[idx])
    lo  = float(lows[idx])
    cl  = float(closes[idx])

    if side == "PE":
        # Price floating ABOVE EMA — alert when candle low > EMA
        if lo <= ema_val:
            return None
        dist_pct = (lo - ema_val) / ema_val
        if dist_pct < min_dist_pct:
            return None
        trigger = lo       # entry on break below alert candle low
        sl      = hi       # SL at alert candle high
        return trigger, sl, ema_val

    else:  # CE
        # Price floating BELOW EMA — alert when candle high < EMA
        if hi >= ema_val:
            return None
        dist_pct = (ema_val - hi) / ema_val
        if dist_pct < min_dist_pct:
            return None
        trigger = hi       # entry on break above alert candle high
        sl      = lo       # SL at alert candle low
        return trigger, sl, ema_val


def _daily_loss_count(config: dict, strategy_name: str) -> int:
    """Read today's SL-hit count for this strategy from config.

    The UserWorker injects 'ema5_losses_today' into the inst.config dict each
    time an EMA5 position is stopped out.  Defaults to 0 if not yet set.
    """
    return int(config.get("ema5_losses_today", 0))


class EMA5MeanReversionStrategy(BaseStrategy):
    """5 EMA mean reversion — buys CE/PE on price exhaustion snap-back."""

    name = "ema5_mean_reversion"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["NSE_INDEX"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        # ── Instrument guard ─────────────────────────────────────────────
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        # ── Circuit breaker ──────────────────────────────────────────────
        daily_limit = int(config.get("daily_loss_limit", 3))
        losses_today = _daily_loss_count(config, self.name)
        if losses_today >= daily_limit:
            return None

        # ── VIX filter ───────────────────────────────────────────────────
        vix = getattr(chain, "india_vix", None) or regime.get("india_vix", 0.0)
        min_vix = float(config.get("min_india_vix", 12.0))
        max_vix = float(config.get("max_india_vix", 35.0))
        if vix > 0 and (vix < min_vix or vix > max_vix):
            return None

        ema_period  = int(config.get("ema_period", 5))
        min_dist    = float(config.get("min_distance_ema_pct", 0.002))
        rr_min      = float(config.get("rr_min", 3.0))
        strike_sel  = config.get("strike_selection", "ATM")

        # ── 5m chart → PE signal (bearish exhaustion) ────────────────────
        data_5m: dict  = chain.candles_5m or {}
        data_15m: dict = getattr(chain, "candles_15m", None) or {}
        data_1m: dict  = chain.candles_1m or {}

        pe_result = None
        if data_5m and "close" in data_5m:
            pe_result = _find_alert_candle(data_5m, ema_period, "PE", min_dist)

        # ── 15m chart → CE signal (bullish exhaustion) ───────────────────
        ce_result = None
        if data_15m and "close" in data_15m:
            ce_result = _find_alert_candle(data_15m, ema_period, "CE", min_dist)

        # Priority: PE first (fear is faster), then CE
        if pe_result is None and ce_result is None:
            return None

        # Determine which signal fired
        if pe_result is not None:
            trigger_price, sl_price, ema_val = pe_result
            direction   = "BEARISH"
            option_type = "PE"
        else:
            trigger_price, sl_price, ema_val = ce_result
            direction   = "BULLISH"
            option_type = "CE"

        # Confirm current price has reached the trigger (momentum confirmation)
        spot = float(data_1m["close"][-1]) if data_1m.get("close") is not None and len(data_1m["close"]) > 0 else float(chain.candles_5m["close"][-1])

        if direction == "BEARISH" and spot > trigger_price:
            return None   # price hasn't broken down yet
        if direction == "BULLISH" and spot < trigger_price:
            return None   # price hasn't broken up yet

        # ── Strike selection ─────────────────────────────────────────────
        dte = self.get_dte(chain)

        if chain.strikes:
            if strike_sel == "ATM":
                strike_data = self.find_atm_strike(chain, option_type)
            else:
                itm_target  = _itm_strike(spot, chain.underlying, option_type)
                strike_data = self.find_strike_near(chain, itm_target, option_type)
                if strike_data is None:
                    strike_data = self.find_atm_strike(chain, option_type)
            if strike_data is None:
                return None
            premium = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
            if premium <= 0:
                premium = _estimate_premium(spot, chain.atm_iv, dte)
            strike_val = strike_data.strike
        else:
            strike_val = _atm_strike(spot, chain.underlying)
            premium    = _estimate_premium(spot, chain.atm_iv, dte)

        if premium <= 0:
            return None

        # ── RR targets ───────────────────────────────────────────────────
        # The risk is the underlying distance from entry to SL, not option premium.
        underlying_risk = abs(trigger_price - sl_price)
        if underlying_risk < 1.0:
            return None   # degenerate signal

        if direction == "BEARISH":
            rr_target_underlying = trigger_price - rr_min * underlying_risk
        else:
            rr_target_underlying = trigger_price + rr_min * underlying_risk

        stop_loss_pct   = 60.0  # backstop
        sl_option_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price    = premium * (1.0 + rr_min * 100.0 / 100.0)   # ≈1:3 RR proxy

        # Time stop: end of trading day (15:20 IST)
        now_ist   = datetime.now(IST)
        eod       = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
        time_stop = eod.astimezone(timezone.utc)

        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        logger.info(
            "ema5_signal",
            underlying=chain.underlying,
            direction=direction,
            trigger=round(trigger_price, 2),
            sl=round(sl_price, 2),
            ema5=round(ema_val, 2),
            rr_target=round(rr_target_underlying, 2),
            option=f"{strike_val}{option_type}",
            premium=round(premium, 2),
        )

        return Signal(
            strategy_name=self.name,
            underlying=chain.underlying,
            segment=config.get("segment", "NSE_INDEX"),
            direction=direction,
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=sl_option_price,
            target_pct=rr_min * 100.0,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=min(0.90, 0.50 + (underlying_risk / spot) * 50),
            metadata={
                "sl_price":              round(sl_price, 2),
                "trigger_price":         round(trigger_price, 2),
                "rr_target_underlying":  round(rr_target_underlying, 2),
                "ema5_val":              round(ema_val, 2),
                "direction":             direction,
            },
        )

    def should_exit(self, position: Position, current_chain, config) -> bool:
        data = current_chain.candles_5m or current_chain.candles_1m
        if not data or "close" not in data:
            return False

        now = datetime.now(timezone.utc)
        if now >= position.time_stop:
            return True

        curr_price = float(data["close"][-1])
        direction  = position.metadata.get("direction", "")
        sl_price   = position.metadata.get("sl_price", 0.0)
        rr_tgt     = position.metadata.get("rr_target_underlying", 0.0)

        # SL: underlying crosses the alert candle boundary
        if sl_price > 0:
            if direction == "BEARISH" and curr_price >= sl_price:
                return True
            if direction == "BULLISH" and curr_price <= sl_price:
                return True

        # Target: 1:3 RR in underlying
        if rr_tgt > 0:
            if direction == "BEARISH" and curr_price <= rr_tgt:
                return True
            if direction == "BULLISH" and curr_price >= rr_tgt:
                return True

        # EMA touch exit: price retraces back to 5 EMA
        ema_period = int(config.get("ema_period", 5))
        closes = data.get("close", [])
        if len(closes) >= ema_period + 2:
            ema = _compute_ema(np.array(closes, dtype=np.float64), ema_period)
            if not np.isnan(ema[-1]):
                ema_now = float(ema[-1])
                if direction == "BEARISH" and curr_price <= ema_now:
                    return True
                if direction == "BULLISH" and curr_price >= ema_now:
                    return True

        return False
