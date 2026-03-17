"""TTMSqueezeStrategy — momentum breakout when Bollinger Bands squeeze inside Keltner Channels.

Entry: squeeze releases with positive (bullish) or negative (bearish) momentum.
Option: ATM CE for BUY, ATM PE for SELL.
Per-instrument filtering via config['instruments'].
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import bollinger_bands, keltner_channels, atr_wilder

logger = structlog.get_logger(service="user_worker_pool", module="ttm_squeeze")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _day_end_stop(now: datetime) -> datetime:
    """15:20 IST = 09:50 UTC same day."""
    return now.replace(hour=9, minute=50, second=0, microsecond=0)


class TTMSqueezeStrategy(BaseStrategy):
    """TTM Squeeze — fires on squeeze release with momentum confirmation."""

    name = "ttm_squeeze"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        # ── instrument filter ─────────────────────────────────────────
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        # ── prevent duplicate positions ───────────────────────────────
        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        # ── candle data ───────────────────────────────────────────────
        data_5m: dict = chain.candles_5m
        data_1m: dict = chain.candles_1m
        if not data_5m or "close" not in data_5m:
            return None

        closes = data_5m["close"]
        highs = data_5m["high"]
        lows = data_5m["low"]

        bb_period = config.get("bb_period", 20)
        bb_std = config.get("bb_std", 2.0)
        kc_period = config.get("kc_period", 20)
        kc_atr_period = config.get("kc_atr_period", 10)
        kc_mult = config.get("kc_mult", 1.5)

        min_len = max(bb_period, kc_period) + kc_atr_period + 5
        if len(closes) < min_len:
            return None

        bb = bollinger_bands(closes, bb_period, bb_std)
        kc = keltner_channels(highs, lows, closes, kc_period, kc_atr_period, kc_mult)
        if not bb["upper"] or not kc["upper"]:
            return None

        min_series = min(len(bb["upper"]), len(kc["upper"]))
        if min_series < 3:
            return None

        bb_upper = bb["upper"][-min_series:]
        bb_lower = bb["lower"][-min_series:]
        kc_upper = kc["upper"][-min_series:]
        kc_lower = kc["lower"][-min_series:]

        def is_squeeze(i: int) -> bool:
            return bb_lower[i] > kc_lower[i] and bb_upper[i] < kc_upper[i]

        was_squeezed = is_squeeze(-3) or is_squeeze(-2)
        released = not is_squeeze(-1)
        if not (was_squeezed and released):
            return None

        # momentum = close – midpoint of (highest high, lowest low) over period
        def _momentum(c: list[float], period: int) -> list[float]:
            mom = []
            for i in range(period - 1, len(c)):
                w = c[i - period + 1: i + 1]
                mid = (max(w) + min(w)) / 2
                mom.append(c[i] - mid)
            return mom

        momentum = _momentum(closes, bb_period)
        if len(momentum) < 2:
            return None

        mom_curr = momentum[-1]
        mom_prev = momentum[-2]

        if mom_curr > 0 and mom_curr > mom_prev:
            signal_dir = "BUY"
        elif mom_curr < 0 and mom_curr < mom_prev:
            signal_dir = "SELL"
        else:
            return None

        # ── entry price ───────────────────────────────────────────────
        spot = data_1m["close"][-1] if data_1m and "close" in data_1m else closes[-1]

        now = datetime.now(timezone.utc)
        time_stop = _day_end_stop(now)
        if time_stop <= now:
            time_stop = now + timedelta(hours=2)

        common_meta = {
            "momentum": round(mom_curr, 2),
            "signal_dir": signal_dir,
            "bandwidth": round(bb["bandwidth"][-1] * 100, 4) if bb["bandwidth"] else 0,
        }

        # ── no options chain → DIRECT signal on underlying price ──────
        if not chain.strikes:
            atr_vals = atr_wilder(highs, lows, closes, 14)
            atr_val = atr_vals[-1] if atr_vals else spot * 0.01
            stop_dist = max(spot * 0.005, atr_val * 1.5)
            target_dist = stop_dist * 2.0
            if signal_dir == "BUY":
                sl_price = round(spot - stop_dist, 2)
                tgt_price = round(spot + target_dist, 2)
            else:
                sl_price = round(spot + stop_dist, 2)
                tgt_price = round(spot - target_dist, 2)
            sl_pct = round(stop_dist / spot * 100, 2)
            tgt_pct = round(target_dist / spot * 100, 2)
            return Signal(
                strategy_name=self.name,
                underlying=chain.underlying,
                segment=config.get("segment", "NSE_INDEX"),
                direction="BULLISH" if signal_dir == "BUY" else "BEARISH",
                legs=[],
                entry_price=round(spot, 2),
                stop_loss_pct=sl_pct,
                stop_loss_price=sl_price,
                target_pct=tgt_pct,
                target_price=tgt_price,
                time_stop=time_stop,
                max_loss_inr=stop_dist,
                expiry=chain.expiry,
                confidence=0.75,
                signal_type="DIRECT",
                metadata={**common_meta, "atr": round(atr_val, 2)},
            )

        # ── options chain present → standard OPTIONS signal ───────────
        option_type = "CE" if signal_dir == "BUY" else "PE"
        dte = self.get_dte(chain)

        strike_data = self.find_atm_strike(chain, option_type)
        if strike_data is None:
            return None
        premium = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
        if premium <= 0:
            premium = _estimate_premium(spot, chain.atm_iv, dte)
        strike_val = strike_data.strike

        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 80.0)
        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

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
            confidence=0.8,
            signal_type="OPTIONS",
            metadata=common_meta,
        )

    def should_exit(self, position, current_chain, config):
        if not current_chain.candles_1m or "close" not in current_chain.candles_1m:
            return False
        curr_price = current_chain.candles_1m["close"][-1]
        return (curr_price <= position.stop_loss_price
                or curr_price >= position.target_price
                or datetime.now(timezone.utc) >= position.time_stop)
