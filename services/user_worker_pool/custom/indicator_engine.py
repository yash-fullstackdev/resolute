"""
IndicatorEngine — maintains rolling OHLCV buffers per symbol and dispatches
indicator computation to the appropriate module.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import structlog

from .indicators import IndicatorConfig, IndicatorResult, IndicatorType
from .indicators.moving_averages import compute_sma, compute_ema, compute_wma, compute_dema
from .indicators.oscillators import (
    compute_rsi,
    compute_stochastic,
    compute_stochastic_rsi,
    compute_cci,
    compute_williams_r,
    compute_mfi,
    compute_roc,
    compute_momentum,
)
from .indicators.trend import (
    compute_macd,
    compute_supertrend,
    compute_parabolic_sar,
    compute_adx,
    compute_ichimoku,
)
from .indicators.volatility import (
    compute_bollinger_bands,
    compute_atr,
    compute_keltner_channel,
    compute_donchian_channel,
)
from .indicators.volume import compute_vwap, compute_obv, compute_ad_line

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# OHLCV candle dataclass
# ---------------------------------------------------------------------------

@dataclass
class OHLCV:
    """A single OHLCV candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


# ---------------------------------------------------------------------------
# Internal buffer (structured NumPy arrays per symbol)
# ---------------------------------------------------------------------------

_BUFFER_DTYPE = np.dtype([
    ("timestamp", "f8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("volume", "f8"),
])


# ---------------------------------------------------------------------------
# IndicatorEngine
# ---------------------------------------------------------------------------

class IndicatorEngine:
    """Computes indicator values for any symbol on demand.

    Maintains a rolling window of OHLCV data per symbol in memory.  Call
    ``update()`` to append new candles and ``compute()`` / ``compute_batch()``
    to evaluate indicators.
    """

    def __init__(self, window_size: int = 200) -> None:
        self.window_size = window_size
        # symbol -> list of OHLCV (capped at window_size)
        self._buffers: dict[str, list[OHLCV]] = defaultdict(list)
        # Optional: chain snapshot storage for options indicators
        self._chain_snapshots: dict[str, dict[str, Any]] = {}
        self._prev_chain_snapshots: dict[str, dict[str, Any]] = {}
        # IV history for IV Rank / Percentile
        self._iv_history: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def update(self, symbol: str, candle: OHLCV) -> None:
        """Append a new candle to the rolling buffer for *symbol*."""
        buf = self._buffers[symbol]
        buf.append(candle)
        if len(buf) > self.window_size:
            self._buffers[symbol] = buf[-self.window_size:]

    def update_chain_snapshot(self, symbol: str, snapshot: dict[str, Any]) -> None:
        """Store the latest option chain snapshot for options indicators."""
        if symbol in self._chain_snapshots:
            self._prev_chain_snapshots[symbol] = self._chain_snapshots[symbol]
        self._chain_snapshots[symbol] = snapshot

    def update_iv(self, symbol: str, iv_value: float) -> None:
        """Append an IV observation for IV Rank / Percentile calculations."""
        self._iv_history[symbol].append(iv_value)
        # Keep at most 252 trading days (1 year)
        if len(self._iv_history[symbol]) > 252:
            self._iv_history[symbol] = self._iv_history[symbol][-252:]

    def buffer_length(self, symbol: str) -> int:
        """Number of candles currently stored for *symbol*."""
        return len(self._buffers.get(symbol, []))

    # ------------------------------------------------------------------
    # Array extraction helpers
    # ------------------------------------------------------------------

    def _arrays(self, symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Extract (open, high, low, close, volume) NumPy arrays from the buffer."""
        buf = self._buffers.get(symbol, [])
        if not buf:
            empty = np.array([], dtype=np.float64)
            return empty, empty, empty, empty, empty

        o = np.array([c.open for c in buf], dtype=np.float64)
        h = np.array([c.high for c in buf], dtype=np.float64)
        l = np.array([c.low for c in buf], dtype=np.float64)
        c = np.array([c.close for c in buf], dtype=np.float64)
        v = np.array([c.volume for c in buf], dtype=np.float64)
        return o, h, l, c, v

    # ------------------------------------------------------------------
    # Single indicator computation
    # ------------------------------------------------------------------

    def compute(self, symbol: str, indicator: IndicatorConfig) -> IndicatorResult:
        """Compute the indicator value for *symbol*'s current state.

        Returns an ``IndicatorResult`` with current + previous values and
        a short history (last 10 values) for trend/crossover detection.
        """
        opens, highs, lows, closes, volumes = self._arrays(symbol)
        buf = self._buffers.get(symbol, [])
        ts = buf[-1].timestamp if buf else datetime.utcnow()
        itype = indicator.indicator_type
        params = indicator.params

        try:
            result = self._dispatch(itype, params, symbol, opens, highs, lows, closes, volumes)
        except Exception:
            log.exception("indicator_compute_error", symbol=symbol, indicator=indicator.label)
            return IndicatorResult(
                label=indicator.label,
                current_value=float("nan"),
                previous_value=float("nan"),
                history=[],
                timestamp=ts,
            )

        return self._pack_result(indicator.label, result, ts)

    def compute_batch(
        self,
        symbols: list[str],
        indicators: list[IndicatorConfig],
    ) -> dict[str, dict[str, IndicatorResult]]:
        """Batch compute all indicators for all symbols.

        Returns ``{symbol: {indicator_label: result}}``.
        """
        results: dict[str, dict[str, IndicatorResult]] = {}
        for symbol in symbols:
            results[symbol] = {}
            for ind in indicators:
                results[symbol][ind.label] = self.compute(symbol, ind)
        return results

    # ------------------------------------------------------------------
    # Dispatch to the correct computation function
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        itype: IndicatorType,
        params: dict[str, Any],
        symbol: str,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
    ) -> Any:
        """Route an indicator type to its computation function and return the
        raw result (scalar, array, tuple of arrays, or dict)."""

        # -- Moving averages -----------------------------------------------
        if itype == IndicatorType.SMA:
            return compute_sma(closes, params.get("period", 20))

        if itype == IndicatorType.EMA:
            return compute_ema(closes, params.get("period", 20))

        if itype == IndicatorType.WMA:
            return compute_wma(closes, params.get("period", 20))

        if itype == IndicatorType.DEMA:
            return compute_dema(closes, params.get("period", 20))

        if itype == IndicatorType.MOVING_AVG_RIBBON:
            periods = params.get("periods", [8, 13, 21, 34, 55, 89])
            ribbon: dict[str, float] = {}
            for p in periods:
                ema = compute_ema(closes, p)
                val = float(ema[-1]) if len(ema) > 0 and not np.isnan(ema[-1]) else float("nan")
                ribbon[f"ema_{p}"] = val
            return ribbon

        # -- Oscillators ---------------------------------------------------
        if itype == IndicatorType.RSI:
            return compute_rsi(closes, params.get("period", 14))

        if itype == IndicatorType.STOCHASTIC:
            k, d = compute_stochastic(
                highs, lows, closes,
                params.get("k_period", 14),
                params.get("d_period", 3),
            )
            return {"k": k, "d": d}

        if itype == IndicatorType.STOCHASTIC_RSI:
            k, d = compute_stochastic_rsi(
                closes,
                params.get("rsi_period", 14),
                params.get("stoch_period", 14),
                params.get("k_smooth", 3),
                params.get("d_smooth", 3),
            )
            return {"k": k, "d": d}

        if itype == IndicatorType.CCI:
            return compute_cci(highs, lows, closes, params.get("period", 20))

        if itype == IndicatorType.WILLIAMS_R:
            return compute_williams_r(highs, lows, closes, params.get("period", 14))

        if itype == IndicatorType.MFI:
            return compute_mfi(highs, lows, closes, volumes, params.get("period", 14))

        if itype == IndicatorType.ROC:
            return compute_roc(closes, params.get("period", 12))

        if itype == IndicatorType.MOMENTUM:
            return compute_momentum(closes, params.get("period", 10))

        # -- Trend ---------------------------------------------------------
        if itype == IndicatorType.MACD or itype == IndicatorType.MACD_HISTOGRAM:
            line, signal, histogram = compute_macd(
                closes,
                params.get("fast", 12),
                params.get("slow", 26),
                params.get("signal", 9),
            )
            return {"line": line, "signal": signal, "histogram": histogram}

        if itype == IndicatorType.SUPERTREND:
            st, direction = compute_supertrend(
                highs, lows, closes,
                params.get("period", 10),
                params.get("multiplier", 3.0),
            )
            return {"value": st, "direction": direction}

        if itype == IndicatorType.PARABOLIC_SAR:
            sar, direction = compute_parabolic_sar(
                highs, lows,
                params.get("af_start", 0.02),
                params.get("af_step", 0.02),
                params.get("af_max", 0.20),
            )
            return {"value": sar, "direction": direction}

        if itype == IndicatorType.ADX:
            adx, plus_di, minus_di = compute_adx(
                highs, lows, closes,
                params.get("period", 14),
            )
            return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}

        if itype == IndicatorType.ICHIMOKU:
            return compute_ichimoku(
                highs, lows, closes,
                params.get("tenkan_period", 9),
                params.get("kijun_period", 26),
                params.get("senkou_b_period", 52),
                params.get("displacement", 26),
            )

        # -- Volatility ----------------------------------------------------
        if itype == IndicatorType.BOLLINGER_BANDS:
            upper, middle, lower, width = compute_bollinger_bands(
                closes,
                params.get("period", 20),
                params.get("num_std", 2.0),
            )
            return {"upper": upper, "middle": middle, "lower": lower, "width": width}

        if itype == IndicatorType.BOLLINGER_WIDTH:
            _, _, _, width = compute_bollinger_bands(
                closes,
                params.get("period", 20),
                params.get("num_std", 2.0),
            )
            return width

        if itype == IndicatorType.ATR:
            return compute_atr(highs, lows, closes, params.get("period", 14))

        if itype == IndicatorType.KELTNER_CHANNEL:
            upper, middle, lower = compute_keltner_channel(
                highs, lows, closes,
                params.get("ema_period", 20),
                params.get("atr_period", 10),
                params.get("multiplier", 1.5),
            )
            return {"upper": upper, "middle": middle, "lower": lower}

        if itype == IndicatorType.DONCHIAN_CHANNEL:
            upper, middle, lower = compute_donchian_channel(
                highs, lows,
                params.get("period", 20),
            )
            return {"upper": upper, "middle": middle, "lower": lower}

        if itype == IndicatorType.INDIA_VIX:
            # Direct pass-through — value must be provided in params
            return params.get("value", float("nan"))

        # -- Volume --------------------------------------------------------
        if itype == IndicatorType.VWAP:
            return compute_vwap(highs, lows, closes, volumes)

        if itype == IndicatorType.OBV:
            return compute_obv(closes, volumes)

        if itype == IndicatorType.AD_LINE:
            return compute_ad_line(highs, lows, closes, volumes)

        if itype == IndicatorType.VOLUME_PROFILE:
            # Volume profile is a distribution — return current volume as scalar
            return volumes

        # -- Options -------------------------------------------------------
        if itype == IndicatorType.IV_RANK:
            from .indicators.options import compute_iv_rank
            iv_hist = np.array(self._iv_history.get(symbol, []), dtype=np.float64)
            current_iv = params.get("current_iv", iv_hist[-1] if len(iv_hist) > 0 else 0.0)
            return compute_iv_rank(current_iv, iv_hist)

        if itype == IndicatorType.IV_PERCENTILE:
            from .indicators.options import compute_iv_percentile
            iv_hist = np.array(self._iv_history.get(symbol, []), dtype=np.float64)
            current_iv = params.get("current_iv", iv_hist[-1] if len(iv_hist) > 0 else 0.0)
            return compute_iv_percentile(current_iv, iv_hist)

        if itype == IndicatorType.PCR_OI:
            from .indicators.options import compute_pcr_oi
            snap = self._chain_snapshots.get(symbol, {})
            return compute_pcr_oi(snap)

        if itype == IndicatorType.PCR_VOLUME:
            from .indicators.options import compute_pcr_volume
            snap = self._chain_snapshots.get(symbol, {})
            return compute_pcr_volume(snap)

        if itype == IndicatorType.MAX_PAIN:
            from .indicators.options import compute_max_pain
            snap = self._chain_snapshots.get(symbol, {})
            return compute_max_pain(snap)

        if itype in (IndicatorType.OI_CHANGE, IndicatorType.CALL_OI_CHANGE, IndicatorType.PUT_OI_CHANGE):
            from .indicators.options import compute_oi_change
            snap = self._chain_snapshots.get(symbol, {})
            prev = self._prev_chain_snapshots.get(symbol)
            oi = compute_oi_change(snap, prev)
            if itype == IndicatorType.CALL_OI_CHANGE:
                return oi["call_oi_change"]
            if itype == IndicatorType.PUT_OI_CHANGE:
                return oi["put_oi_change"]
            return oi

        if itype == IndicatorType.IV_SKEW:
            from .indicators.options import compute_iv_skew
            snap = self._chain_snapshots.get(symbol, {})
            price = params.get("underlying_price", 0.0)
            distance = params.get("distance_pct", 5.0)
            return compute_iv_skew(snap, price, distance)

        log.warning("unknown_indicator_type", indicator_type=itype.value)
        return float("nan")

    # ------------------------------------------------------------------
    # Pack raw computation output into IndicatorResult
    # ------------------------------------------------------------------

    def _pack_result(
        self,
        label: str,
        raw: Any,
        timestamp: datetime,
    ) -> IndicatorResult:
        """Convert raw computation output into an ``IndicatorResult``."""

        # Scalar (options indicators, INDIA_VIX, etc.)
        if isinstance(raw, (int, float)):
            return IndicatorResult(
                label=label,
                current_value=float(raw),
                previous_value=float(raw),
                history=[float(raw)],
                timestamp=timestamp,
            )

        # Single NumPy array (simple indicators: SMA, RSI, ATR, etc.)
        if isinstance(raw, np.ndarray) and raw.ndim == 1:
            return self._pack_array(label, raw, timestamp)

        # Dict of arrays (complex indicators: MACD, Bollinger, etc.)
        if isinstance(raw, dict):
            # Check if values are arrays or scalars
            first_val = next(iter(raw.values()), None)
            if isinstance(first_val, np.ndarray):
                return self._pack_dict_of_arrays(label, raw, timestamp)
            # Dict of scalars (e.g. OI change, ribbon)
            return IndicatorResult(
                label=label,
                current_value=raw,
                previous_value=raw,
                history=[raw],
                timestamp=timestamp,
            )

        # Fallback
        return IndicatorResult(
            label=label,
            current_value=float("nan"),
            previous_value=float("nan"),
            history=[],
            timestamp=timestamp,
        )

    @staticmethod
    def _pack_array(label: str, arr: np.ndarray, timestamp: datetime) -> IndicatorResult:
        """Pack a 1-D NumPy array into IndicatorResult with current, previous,
        and last-10 history."""
        valid = arr[~np.isnan(arr)]
        if len(valid) == 0:
            return IndicatorResult(
                label=label,
                current_value=float("nan"),
                previous_value=float("nan"),
                history=[],
                timestamp=timestamp,
            )

        current = float(valid[-1])
        previous = float(valid[-2]) if len(valid) >= 2 else current
        history = [float(v) for v in valid[-10:]]

        return IndicatorResult(
            label=label,
            current_value=current,
            previous_value=previous,
            history=history,
            timestamp=timestamp,
        )

    @staticmethod
    def _pack_dict_of_arrays(
        label: str,
        raw: dict[str, np.ndarray],
        timestamp: datetime,
    ) -> IndicatorResult:
        """Pack a dict of NumPy arrays into IndicatorResult.

        ``current_value`` and ``previous_value`` become dicts of floats.
        ``history`` is a list of dicts (last 10 bars).
        """
        current: dict[str, float] = {}
        previous: dict[str, float] = {}

        for key, arr in raw.items():
            valid = arr[~np.isnan(arr)]
            if len(valid) == 0:
                current[key] = float("nan")
                previous[key] = float("nan")
            else:
                current[key] = float(valid[-1])
                previous[key] = float(valid[-2]) if len(valid) >= 2 else float(valid[-1])

        # Build history: list of dicts for last 10 valid bars
        # Use the first key to determine history length
        first_key = next(iter(raw))
        first_valid = raw[first_key][~np.isnan(raw[first_key])]
        hist_len = min(10, len(first_valid))

        history: list[dict[str, float]] = []
        for i in range(hist_len):
            point: dict[str, float] = {}
            for key, arr in raw.items():
                valid = arr[~np.isnan(arr)]
                idx = len(valid) - hist_len + i
                if 0 <= idx < len(valid):
                    point[key] = float(valid[idx])
                else:
                    point[key] = float("nan")
            history.append(point)

        return IndicatorResult(
            label=label,
            current_value=current,
            previous_value=previous,
            history=history,
            timestamp=timestamp,
        )
