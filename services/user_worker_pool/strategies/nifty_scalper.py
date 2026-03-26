"""NiftyScalperStrategy — NIFTY index options scalper using 6 validated 1-minute candle patterns.

Scores each 1-minute bar against volume spike, PDC sniper, engulfing, micro pullback,
momentum burst, and inside bar breakout patterns.  Fires BUY CE / SELL PE when the
cumulative score meets min_score.  Exits via SL/TP/time_stop.

Based on 3-year cross-validated analysis of NIFTY 1-minute data (788 trading days).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

logger = structlog.get_logger(service="user_worker_pool", module="nifty_scalper")


# ---------------------------------------------------------------------------
# Default configuration — every key is tunable from the strategies UI
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Pattern weights (set to 0 to disable any pattern)
    "volume_spike_weight": 5,
    "pdc_sniper_weight": 4,
    "engulfing_weight": 3,
    "micro_pullback_weight": 3,
    "momentum_burst_weight": 2,
    "inside_bar_weight": 2,
    # Score threshold
    "min_score": 3,
    # Pattern tuning
    "engulfing_min_body": 10,
    "engulfing_body_ratio": 0.6,
    "pullback_trend_pts": 15,
    "momentum_min_range": 5,
    "inside_bar_min_range": 8,
    # Risk management
    "sl_points": 15,
    "tp_points": 25,
    "max_hold_minutes": 15,
    "strike_selection": "ATM",
    # Trade management
    "max_trades_per_day": 5,
    "min_gap_between_trades": 3,
}


# ---------------------------------------------------------------------------
# Helper — build bar dicts from chain candle arrays
# ---------------------------------------------------------------------------

def _build_bars(candles: dict, count: int = 20) -> list[dict]:
    """Convert chain candle arrays into a list of bar dicts.

    Returns the last *count* bars.  Each bar has keys:
    o, h, l, c, v, body, bar_range, is_bull, is_bear.
    """
    opens = candles.get("open", [])
    highs = candles.get("high", [])
    lows = candles.get("low", [])
    closes = candles.get("close", [])
    volumes = candles.get("volume", [])

    n = min(len(opens), len(highs), len(lows), len(closes))
    if n == 0:
        return []

    # Pad volumes with zeros if missing or shorter
    if not volumes or len(volumes) < n:
        volumes = [0] * n

    start = max(0, n - count)
    bars: list[dict] = []
    for idx in range(start, n):
        o = opens[idx]
        h = highs[idx]
        l = lows[idx]
        c = closes[idx]
        v = volumes[idx]
        body = abs(c - o)
        bar_range = h - l
        bars.append({
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "v": v,
            "body": body,
            "range": bar_range,
            "is_bull": c > o,
            "is_bear": c < o,
        })
    return bars


# ---------------------------------------------------------------------------
# Premium estimation helper
# ---------------------------------------------------------------------------

def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    """Rough ATM premium estimate using simplified BS approximation."""
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


# ---------------------------------------------------------------------------
# 6 Pattern Detectors
# Each returns (buy_score, sell_score).
# ---------------------------------------------------------------------------

def _detect_volume_spike(
    bars: list[dict], i: int, config: dict,
) -> tuple[int, int]:
    """Bar volume > 3x running avg (last 20 bars), body > 10pts, body > 70% range.

    Gracefully returns (0, 0) when volume data is unavailable (v == 0).
    """
    weight = config.get("volume_spike_weight", 5)
    if weight == 0:
        return (0, 0)

    curr = bars[i]
    if curr["v"] == 0:
        return (0, 0)

    # Running average over preceding bars (up to 20)
    lookback = min(i, 20)
    if lookback == 0:
        return (0, 0)

    vol_sum = 0
    vol_count = 0
    for j in range(i - lookback, i):
        if bars[j]["v"] > 0:
            vol_sum += bars[j]["v"]
            vol_count += 1

    if vol_count == 0 or vol_sum == 0:
        return (0, 0)

    avg_vol = vol_sum / vol_count

    if curr["v"] <= avg_vol * 3:
        return (0, 0)
    if curr["body"] <= 10:
        return (0, 0)
    if curr["range"] <= 0 or curr["body"] / curr["range"] <= 0.70:
        return (0, 0)

    if curr["is_bull"]:
        return (weight, 0)
    elif curr["is_bear"]:
        return (0, weight)
    return (0, 0)


def _detect_pdc_sniper(
    bars: list[dict],
    i: int,
    pdc: float | None,
    pdc_touched: dict,
    config: dict,
) -> tuple[int, int]:
    """First touch of previous day close within 5pts — rejects with directional bar.

    One-shot per day per direction.  *pdc_touched* is mutated to track state:
    ``{"buy": False, "sell": False}``.
    """
    weight = config.get("pdc_sniper_weight", 4)
    if weight == 0 or pdc is None:
        return (0, 0)

    curr = bars[i]
    tolerance = 5

    # Bullish: price dips to PDC from above, bar closes above PDC with bullish body
    if not pdc_touched.get("buy", False):
        if curr["l"] <= pdc + tolerance and curr["c"] > pdc and curr["is_bull"]:
            pdc_touched["buy"] = True
            return (weight, 0)

    # Bearish: price rallies to PDC from below, bar closes below PDC with bearish body
    if not pdc_touched.get("sell", False):
        if curr["h"] >= pdc - tolerance and curr["c"] < pdc and curr["is_bear"]:
            pdc_touched["sell"] = True
            return (0, weight)

    return (0, 0)


def _detect_engulfing(
    bars: list[dict], i: int, config: dict,
) -> tuple[int, int]:
    """Current bar body engulfs previous bar body.

    Body > engulfing_min_body pts.  Body/range > engulfing_body_ratio.
    """
    weight = config.get("engulfing_weight", 3)
    if weight == 0 or i < 1:
        return (0, 0)

    prev = bars[i - 1]
    curr = bars[i]
    min_body = config.get("engulfing_min_body", 10)
    body_ratio = config.get("engulfing_body_ratio", 0.6)

    if curr["body"] < min_body:
        return (0, 0)
    if curr["range"] <= 0 or curr["body"] / curr["range"] < body_ratio:
        return (0, 0)

    # Bullish engulfing: prev bearish, curr bullish, curr body engulfs prev body
    if prev["is_bear"] and curr["is_bull"]:
        if curr["c"] > prev["o"] and curr["o"] < prev["c"]:
            return (weight, 0)

    # Bearish engulfing: prev bullish, curr bearish, curr body engulfs prev body
    if prev["is_bull"] and curr["is_bear"]:
        if curr["c"] < prev["o"] and curr["o"] > prev["c"]:
            return (0, weight)

    return (0, 0)


def _detect_micro_pullback(
    bars: list[dict], i: int, config: dict,
) -> tuple[int, int]:
    """Trend bars[i-5:i-2], pullback bars[i-2:i], continuation bar[i].

    Trend: 2+ same-direction bars with move > pullback_trend_pts.
    Pullback: 1+ opposite bar.
    Continuation: bar[i] closes beyond previous bar extreme in trend direction.
    """
    weight = config.get("micro_pullback_weight", 3)
    if weight == 0 or i < 5:
        return (0, 0)

    trend_pts = config.get("pullback_trend_pts", 15)
    trend_bars = bars[i - 5 : i - 2]
    pullback_bars = bars[i - 2 : i]
    curr = bars[i]

    # --- Bullish micro pullback ---
    bull_count = sum(1 for b in trend_bars if b["is_bull"])
    if bull_count >= 2:
        trend_move = trend_bars[-1]["c"] - trend_bars[0]["o"]
        if trend_move > trend_pts:
            has_bearish_pullback = any(b["is_bear"] for b in pullback_bars)
            if has_bearish_pullback:
                if curr["is_bull"] and curr["c"] > bars[i - 1]["h"]:
                    return (weight, 0)

    # --- Bearish micro pullback ---
    bear_count = sum(1 for b in trend_bars if b["is_bear"])
    if bear_count >= 2:
        trend_move = trend_bars[0]["o"] - trend_bars[-1]["c"]
        if trend_move > trend_pts:
            has_bullish_pullback = any(b["is_bull"] for b in pullback_bars)
            if has_bullish_pullback:
                if curr["is_bear"] and curr["c"] < bars[i - 1]["l"]:
                    return (0, weight)

    return (0, 0)


def _detect_momentum_burst(
    bars: list[dict], i: int, config: dict,
) -> tuple[int, int]:
    """3 consecutive bars same direction, each body > 50% range, each range > momentum_min_range."""
    weight = config.get("momentum_burst_weight", 2)
    if weight == 0 or i < 2:
        return (0, 0)

    min_range = config.get("momentum_min_range", 5)
    trio = [bars[i - 2], bars[i - 1], bars[i]]

    # Check all three bars have sufficient range and body ratio
    for b in trio:
        if b["range"] < min_range:
            return (0, 0)
        if b["range"] <= 0 or b["body"] / b["range"] <= 0.5:
            return (0, 0)

    # All bullish
    if all(b["is_bull"] for b in trio):
        return (weight, 0)
    # All bearish
    if all(b["is_bear"] for b in trio):
        return (0, weight)

    return (0, 0)


def _detect_inside_bar(
    bars: list[dict], i: int, config: dict,
) -> tuple[int, int]:
    """bar[i-1] is inside bar (H/L within bar[i-2]).  bar[i] breaks mother bar.

    Mother (bar[i-2]) range > inside_bar_min_range.
    BUY if bar[i] breaks mother high.  SELL if bar[i] breaks mother low.
    """
    weight = config.get("inside_bar_weight", 2)
    if weight == 0 or i < 2:
        return (0, 0)

    min_range = config.get("inside_bar_min_range", 8)
    mother = bars[i - 2]
    inside = bars[i - 1]
    curr = bars[i]

    if mother["range"] < min_range:
        return (0, 0)

    # Inside bar condition: bar[i-1] H/L within mother H/L
    if inside["h"] > mother["h"] or inside["l"] < mother["l"]:
        return (0, 0)

    # Breakout on current bar
    if curr["h"] > mother["h"]:
        return (weight, 0)
    if curr["l"] < mother["l"]:
        return (0, weight)

    return (0, 0)


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

class NiftyScalperStrategy(BaseStrategy):
    """NIFTY index options scalper — 6-pattern scoring on 1-minute bars."""

    name = "nifty_scalper"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX"]
    requires_margin = False

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------

    def evaluate(
        self,
        chain,
        regime,
        open_positions: list[Position],
        config: dict,
    ) -> Signal | None:
        cfg = {**DEFAULT_CONFIG, **config}

        # ── instrument filter ─────────────────────────────────────────
        instruments = cfg.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        # ── prevent duplicate positions ───────────────────────────────
        if self.has_existing_position(self.name, chain.underlying, open_positions):
            logger.debug(
                "skip_existing_position",
                strategy=self.name,
                underlying=chain.underlying,
            )
            return None

        # ── candle data ───────────────────────────────────────────────
        data_1m: dict | None = chain.candles_1m
        if not data_1m or "close" not in data_1m:
            return None

        bars = _build_bars(data_1m, count=20)
        if len(bars) < 6:
            logger.debug(
                "skip_insufficient_bars",
                strategy=self.name,
                n_bars=len(bars),
            )
            return None

        # ── trade management checks ───────────────────────────────────
        meta = cfg.get("_metadata", {})
        trades_today = meta.get("trades_today", 0)
        last_trade_time = meta.get("last_trade_time")  # datetime or None
        max_trades = cfg.get("max_trades_per_day", 5)
        min_gap = cfg.get("min_gap_between_trades", 3)

        if trades_today >= max_trades:
            logger.debug(
                "skip_max_trades",
                strategy=self.name,
                trades_today=trades_today,
            )
            return None

        now = datetime.now(timezone.utc)
        if last_trade_time is not None:
            if isinstance(last_trade_time, str):
                last_trade_time = datetime.fromisoformat(last_trade_time)
            elapsed = (now - last_trade_time).total_seconds() / 60.0
            if elapsed < min_gap:
                logger.debug(
                    "skip_cooldown",
                    strategy=self.name,
                    elapsed_min=round(elapsed, 1),
                    min_gap=min_gap,
                )
                return None

        # ── PDC derivation ────────────────────────────────────────────
        pdc: float | None = None
        if hasattr(chain, "prev_day_close") and chain.prev_day_close:
            pdc = chain.prev_day_close
        elif hasattr(chain, "candles_1d") and chain.candles_1d:
            day_closes = chain.candles_1d.get("close", [])
            if len(day_closes) >= 2:
                pdc = day_closes[-2]

        pdc_touched: dict = meta.get("pdc_touched", {"buy": False, "sell": False})

        # ── score all 6 patterns ──────────────────────────────────────
        i = len(bars) - 1  # evaluate the latest bar
        buy_score = 0
        sell_score = 0
        patterns_fired: list[str] = []

        detectors = [
            ("volume_spike", lambda: _detect_volume_spike(bars, i, cfg)),
            ("pdc_sniper", lambda: _detect_pdc_sniper(bars, i, pdc, pdc_touched, cfg)),
            ("engulfing", lambda: _detect_engulfing(bars, i, cfg)),
            ("micro_pullback", lambda: _detect_micro_pullback(bars, i, cfg)),
            ("momentum_burst", lambda: _detect_momentum_burst(bars, i, cfg)),
            ("inside_bar", lambda: _detect_inside_bar(bars, i, cfg)),
        ]

        for name, detect_fn in detectors:
            bs, ss = detect_fn()
            if bs > 0:
                buy_score += bs
                patterns_fired.append(f"{name}:BUY")
            if ss > 0:
                sell_score += ss
                patterns_fired.append(f"{name}:SELL")

        min_score = cfg.get("min_score", 3)

        # ── determine direction ───────────────────────────────────────
        direction: str | None = None
        option_type: str | None = None
        score: int = 0

        if buy_score >= min_score and sell_score >= min_score:
            # Both meet threshold — take the higher; tie = no signal
            if buy_score > sell_score:
                direction = "BULLISH"
                option_type = "CE"
                score = buy_score
            elif sell_score > buy_score:
                direction = "BEARISH"
                option_type = "PE"
                score = sell_score
            else:
                logger.debug(
                    "skip_tied_score",
                    strategy=self.name,
                    buy_score=buy_score,
                    sell_score=sell_score,
                )
                return None
        elif buy_score >= min_score:
            direction = "BULLISH"
            option_type = "CE"
            score = buy_score
        elif sell_score >= min_score:
            direction = "BEARISH"
            option_type = "PE"
            score = sell_score
        else:
            logger.debug(
                "skip_low_score",
                strategy=self.name,
                buy_score=buy_score,
                sell_score=sell_score,
                min_score=min_score,
            )
            return None

        # ── find strike ───────────────────────────────────────────────
        spot = bars[-1]["c"]
        strike_sel = cfg.get("strike_selection", "ATM")

        if not chain.strikes:
            # No options chain available — cannot produce an OPTIONS signal
            return None

        if strike_sel == "ATM":
            strike_data = self.find_atm_strike(chain, option_type)
        elif strike_sel in ("1_OTM", "2_OTM"):
            steps = int(strike_sel[0])
            strike_data = self.find_otm_strike(chain, option_type, steps=steps)
        else:
            strike_data = self.find_atm_strike(chain, option_type)

        if strike_data is None:
            return None

        premium = (
            strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
        )
        dte = self.get_dte(chain)
        if premium <= 0:
            premium = _estimate_premium(spot, getattr(chain, "atm_iv", 0.15), dte)

        strike_val = strike_data.strike

        # ── SL / TP / time_stop ───────────────────────────────────────
        sl_points = cfg.get("sl_points", 15)
        tp_points = cfg.get("tp_points", 25)
        max_hold = cfg.get("max_hold_minutes", 15)

        # Convert index points to approximate premium change using delta
        # ATM option delta ~ 0.5 for CE, -0.5 for PE
        approx_delta = 0.5
        sl_premium = premium - (sl_points * approx_delta)
        tp_premium = premium + (tp_points * approx_delta)
        sl_premium = max(0.05, round(sl_premium, 2))
        tp_premium = round(tp_premium, 2)

        sl_pct = round((premium - sl_premium) / premium * 100, 2) if premium > 0 else 40.0
        tp_pct = round((tp_premium - premium) / premium * 100, 2) if premium > 0 else 80.0

        time_stop = now + timedelta(minutes=max_hold)

        # ── confidence ────────────────────────────────────────────────
        max_possible = 19  # all 6 patterns aligned
        confidence = round(min(score / max_possible, 1.0), 3)

        # ── build signal ──────────────────────────────────────────────
        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        signal = Signal(
            strategy_name=self.name,
            underlying=chain.underlying,
            segment=cfg.get("segment", "NSE_INDEX"),
            direction=direction,
            legs=[leg],
            entry_price=round(premium, 2),
            stop_loss_pct=sl_pct,
            stop_loss_price=sl_premium,
            target_pct=tp_pct,
            target_price=tp_premium,
            time_stop=time_stop,
            max_loss_inr=round(premium, 2),
            expiry=chain.expiry,
            confidence=confidence,
            signal_type="OPTIONS",
            metadata={
                "score": score,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "patterns_fired": patterns_fired,
                "spot": round(spot, 2),
                "strike": strike_val,
                "premium": round(premium, 2),
                "sl_points": sl_points,
                "tp_points": tp_points,
                "strike_selection": strike_sel,
            },
        )

        logger.info(
            "signal_fired",
            strategy=self.name,
            underlying=chain.underlying,
            direction=direction,
            score=score,
            patterns_fired=patterns_fired,
            entry_price=round(premium, 2),
            strike=strike_val,
            confidence=confidence,
        )

        # Update metadata for trade management tracking
        meta["pdc_touched"] = pdc_touched

        return signal

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: Position,
        current_chain,
        config: dict,
    ) -> bool:
        cfg = {**DEFAULT_CONFIG, **config}
        now = datetime.now(timezone.utc)

        # Time stop
        if now >= position.time_stop:
            logger.info(
                "exit_time_stop",
                strategy=self.name,
                position_id=position.position_id,
            )
            return True

        # Premium-based SL/TP
        if not position.legs:
            return False

        leg = position.legs[0]
        entry_premium = leg.premium
        if entry_premium <= 0:
            return False

        # Get current premium from chain
        current_premium: float | None = None
        if current_chain.strikes:
            strike_data = self.find_strike_near(
                current_chain, leg.strike, leg.option_type,
            )
            if strike_data is not None:
                current_premium = (
                    strike_data.call_ltp
                    if leg.option_type == "CE"
                    else strike_data.put_ltp
                )

        if current_premium is None or current_premium <= 0:
            return False

        # Stop loss hit
        if current_premium <= position.stop_loss_price:
            logger.info(
                "exit_stop_loss",
                strategy=self.name,
                position_id=position.position_id,
                entry_premium=entry_premium,
                current_premium=current_premium,
                stop_loss_price=position.stop_loss_price,
            )
            return True

        # Target hit
        if current_premium >= position.target_price:
            logger.info(
                "exit_target",
                strategy=self.name,
                position_id=position.position_id,
                entry_premium=entry_premium,
                current_premium=current_premium,
                target_price=position.target_price,
            )
            return True

        return False
