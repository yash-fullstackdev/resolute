"""
LegacyStrategyAdapter — bridges existing BaseStrategy subclasses to the
Rust backtest engine.

IMPORTANT: Backtesting runs on INDEX POINTS, not option premiums.
Strategies generate BUY/SELL signals, and we trade the underlying index
with ATR-based SL/TP (matching the reference backtest_ref.py approach).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

# Max lookback bars passed to strategy — prevents O(n²) indicator computation.
_MAX_LOOKBACK_1M = 500
_MAX_LOOKBACK_TF = 200

# ── ATR-based exit config (matches backtest_ref.py SCALP_CONFIG) ─────────────
_SL_ATR_MULT = 0.5        # SL = 0.5 × ATR(14) on 5m
_TP_ATR_MULT = 1.5        # TP = 1.5 × ATR(14) on 5m
_MAX_HOLD_BARS = 20       # Time-stop after 20 × 1m candles
_SLIPPAGE_PTS = 0.5       # Slippage in index points

# Per-instrument SL caps (max SL in index points)
_SL_CAPS = {
    "NIFTY_50": 20,
    "BANK_NIFTY": 40,
}


# ── ATR computation ──────────────────────────────────────────────────────────

def _compute_atr(highs, lows, closes, period: int = 14) -> float | None:
    """Compute ATR(period) using Wilder's smoothing on arrays (list or numpy)."""
    n = len(closes)
    if n < period + 1:
        return None
    # True Range
    tr = []
    for i in range(1, n):
        tr.append(max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        ))
    if len(tr) < period:
        return None
    atr = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr


# ── Mock objects ──────────────────────────────────────────────────────────────

class MockStrike:
    def __init__(self, strike: float, spot: float, atm_iv: float):
        self.strike = strike
        dte = 15
        T = max(dte, 1) / 365.0
        premium = max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))
        self.call_ltp = premium
        self.put_ltp = premium


class MockChainSnapshot:
    """Simulates the OptionsChainSnapshot interface consumed by existing strategies."""

    def __init__(
        self,
        underlying: str,
        candles_1m: dict[str, np.ndarray],
        candles_tf: dict[str, np.ndarray],
        atm_iv: float = 0.15,
        bar_date: date | None = None,
    ):
        self.underlying = underlying
        self.underlying_price: float = float(candles_1m["close"][-1]) if len(candles_1m["close"]) > 0 else 0.0

        # Synthetic volume: index data has zero volume — replace with 1.0 so
        # volume-ratio checks in strategies pass (ratio = current/avg = 1.0).
        def _fix_volume(d: dict) -> dict:
            v = d.get("volume", np.array([]))
            if len(v) > 0 and float(np.max(v)) == 0.0:
                d = {**d, "volume": np.ones(len(v), dtype=np.float64)}
            return d

        self.candles_1m: dict = _fix_volume(candles_1m)
        self.candles_5m: dict = _fix_volume(candles_tf)
        self.atm_iv: float = atm_iv
        self._bar_date = bar_date or date.today()
        self.expiry: date = self._next_expiry()
        self.today: date = self._bar_date

        interval = 100 if ("BANK" in underlying or "SENSEX" in underlying) else 50
        atm = round(self.underlying_price / interval) * interval if self.underlying_price > 0 else 0
        self.strikes = [MockStrike(atm, self.underlying_price, atm_iv)] if self.underlying_price > 0 else []

        # Additional attributes accessed by options strategies
        self.iv_rank: float = 35.0
        self.pcr_oi: float = 0.85
        self.vix: float = atm_iv * 100
        self.call_iv: float = atm_iv
        self.put_iv: float = atm_iv * 1.05
        self.near_expiry_iv: float = atm_iv
        self.far_expiry_iv: float = atm_iv * 0.9
        self.next_event_days: int = 3

        price = self.underlying_price or 1.0
        self.high_20d: float = price * 1.05
        self.low_20d: float = price * 0.95
        self.range_5d_pct: float = 3.0

    def _next_expiry(self) -> date:
        """Return nearest Thursday expiry from the bar's date (not wall clock)."""
        d = self._bar_date
        days_until_thursday = (3 - d.weekday()) % 7
        if days_until_thursday == 0:
            days_until_thursday = 7
        return d + timedelta(days=days_until_thursday)


class MockRegime:
    _REGIME_CYCLE = [
        "BULL_LOW_VOL", "BULL_HIGH_VOL", "SIDEWAYS",
        "BEAR_LOW_VOL", "BEAR_RISING_VOL", "PRE_EVENT",
    ]

    def __init__(self, classification: str = "BULL_LOW_VOL", vix_level: float = 16.0):
        self.classification = classification
        self.value = classification
        self.vix_level = vix_level
        self.trend_strength = 0.5
        self.iv_rank = 40.0
        self.pcr = 1.0


class MockPosition:
    def __init__(self, pos_dict: dict):
        self.position_id = str(pos_dict.get("id", 0))
        self.tenant_id = "backtest"
        self.strategy_name = pos_dict.get("strategy_name", "")
        self.underlying = "NIFTY_50"
        self.segment = "NSE_INDEX"
        self.legs = []
        self.entry_time = datetime.now(timezone.utc)
        self.entry_cost_inr = float(pos_dict.get("entry_price", 0))
        self.current_value_inr = float(pos_dict.get("entry_price", 0))
        self.stop_loss_price = float(pos_dict.get("stop_loss", 0))
        self.target_price = float(pos_dict.get("target", 0))
        self.time_stop = datetime.now(timezone.utc) + timedelta(hours=2)
        self.lots = pos_dict.get("quantity", 1)
        self.status = "OPEN"


# ── Adapter ───────────────────────────────────────────────────────────────────

class LegacyStrategyAdapter:
    """
    Wraps an existing BaseStrategy to work with the Rust backtest engine.

    KEY DESIGN: Strategies signal BUY/SELL direction. We trade the underlying
    INDEX (not option premiums). SL/TP are ATR-based on the spot price,
    matching the reference backtest_ref.py approach.
    """

    def __init__(
        self,
        strategy: Any,
        config: dict,
        instrument: str = "NIFTY_50",
        atm_iv_estimate: float = 0.15,
        primary_tf: int = 5,
        max_hold_bars: int = 20,
    ):
        self.strategy = strategy
        self.config = {**config, "instruments": [instrument]}
        self.instrument = instrument
        self.atm_iv = atm_iv_estimate
        # Clamp to minimum 5m — all strategies use 5m candles for signals.
        # With 1m TF, the engine would call us 150k times (2yr) but we only
        # evaluate at 5m closes anyway, wasting 80% of calls on PyO3 overhead.
        self.primary_tf = max(primary_tf, 5)
        self.max_hold_bars = max_hold_bars
        self._prev_bar_day: int = -1
        self._current_bar_date: date = date.today()

        # Patch get_dte ONCE to use bar_date instead of date.today()
        _adapter = self

        @staticmethod
        def _backtest_get_dte(chain):
            return max(0, (chain.expiry - _adapter._current_bar_date).days)

        self._original_get_dte = strategy.__class__.get_dte
        strategy.__class__.get_dte = _backtest_get_dte
        self._get_dte_patched = True

    def evaluate_bar(
        self,
        bar_idx: int,
        tf_idx: int,
        candles_1m_full: dict,
        candles_tf_full: dict,
        open_positions: list,
    ) -> dict | None:
        # ── Resolve bar date from timestamp ──────────────────────────────────
        ts_arr = candles_1m_full.get("timestamp")
        bar_ts: float = float(ts_arr[bar_idx]) if ts_arr is not None and bar_idx < len(ts_arr) else 0.0
        IST_OFFSET = 330 * 60
        ist_day = int(bar_ts + IST_OFFSET) // 86400
        try:
            bar_date = date.fromordinal(date(1970, 1, 1).toordinal() + ist_day)
        except Exception:
            bar_date = date.today()

        # ── Reset strategy's daily state when date changes ────────────────────
        if ist_day != self._prev_bar_day:
            self._prev_bar_day = ist_day
            if hasattr(self.strategy, '_last_date'):
                self.strategy._last_date = {}
            if hasattr(self.strategy, '_fires'):
                self.strategy._fires = {}
            if hasattr(self.strategy, '_daily_trades'):
                self.strategy._daily_trades = {}
            if hasattr(self.strategy, '_last_bar_date'):
                self.strategy._last_bar_date = bar_date

        # ── Slice to current bar with capped lookback ────────────────────────
        start_1m = max(0, bar_idx + 1 - _MAX_LOOKBACK_1M)
        start_tf = max(0, tf_idx + 1 - _MAX_LOOKBACK_TF)
        slice_1m = {k: v[start_1m:bar_idx + 1] for k, v in candles_1m_full.items()}

        if len(slice_1m.get("close", [])) == 0:
            return None

        # ── Slice TF candles (pre-built by engine, always >= 5m) ─────────────
        slice_tf = {k: v[start_tf:tf_idx + 1] for k, v in candles_tf_full.items()}

        # Update bar date for the get_dte patch
        self._current_bar_date = bar_date

        chain = MockChainSnapshot(
            underlying=self.instrument,
            candles_1m=slice_1m,
            candles_tf=slice_tf,
            atm_iv=self.atm_iv,
            bar_date=bar_date,
        )
        regime = MockRegime()
        legacy_positions = [MockPosition(p) for p in open_positions]

        try:
            signal = self.strategy.evaluate(chain, regime, legacy_positions, self.config)
        except Exception:
            signal = None

        if signal is None:
            return None

        return self._translate_signal(signal, chain, slice_tf, bar_ts)

    def _translate_signal(
        self,
        signal: Any,
        chain: MockChainSnapshot,
        slice_tf: dict,
        bar_ts: float = 0.0,
    ) -> dict | None:
        """Translate a strategy signal to an INDEX POINT trade.

        Instead of using the strategy's option premium entry/SL/TP, we:
        1. Enter at the current SPOT price (index level)
        2. Compute ATR(14) from the 5m candles
        3. Set SL = 0.5 × ATR, TP = 1.5 × ATR (capped per instrument)
        4. Direction: BUY (+1) or SELL (-1) from the strategy signal
        """
        spot = chain.underlying_price
        if spot <= 0:
            return None

        # Direction from strategy signal
        direction = 1 if signal.direction in ("BULLISH", "BUY") else -1

        # Compute ATR(14) from 5m candles for SL/TP
        tf_highs = slice_tf.get("high", [])
        tf_lows = slice_tf.get("low", [])
        tf_closes = slice_tf.get("close", [])
        atr = _compute_atr(tf_highs, tf_lows, tf_closes, 14)
        if atr is None or atr <= 0:
            return None

        # ATR-based SL/TP on index points
        sl_cap = _SL_CAPS.get(self.instrument, 30)
        sl_dist = min(_SL_ATR_MULT * atr, sl_cap)
        tp_dist = _TP_ATR_MULT * atr

        entry_price = spot + (_SLIPPAGE_PTS if direction == 1 else -_SLIPPAGE_PTS)

        if direction == 1:  # BUY
            stop_loss = entry_price - sl_dist
            target = entry_price + tp_dist
        else:               # SELL
            stop_loss = entry_price + sl_dist
            target = entry_price - tp_dist

        return {
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target": target,
            "quantity": 1,
            "time_stop_bars": self.max_hold_bars,
            "tag": getattr(signal, "direction", "signal"),
        }
