"""
ChainProcessor — maintains in-memory option chain state and computes Greeks.

Responsibilities:
  - Accept tick updates and upsert into per-underlying chain state
  - On demand or periodically, compute Greeks for all strikes (vectorised BS)
  - Build full OptionsChainSnapshot with PCR, ATM IV, IV Rank
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Optional

import numpy as np
import structlog

from .greeks import black_scholes_vectorised, moneyness_mask
from .iv_calculator import newton_raphson_iv, calculate_iv_rank
from .pcr import calculate_pcr

logger = structlog.get_logger(service="signal_engine", module="chain_processor")

# RBI repo rate as of 2025
RISK_FREE_RATE = 0.065


# ---------------------------------------------------------------------------
# Data models (kept local to avoid circular imports; mirrors spec exactly)
# ---------------------------------------------------------------------------

@dataclass
class StrikeData:
    strike: float
    call_ltp: float = 0.0
    call_iv: float = 0.0
    call_delta: float = 0.0
    call_gamma: float = 0.0
    call_theta: float = 0.0
    call_vega: float = 0.0
    call_oi: int = 0
    call_volume: int = 0
    put_ltp: float = 0.0
    put_iv: float = 0.0
    put_delta: float = 0.0
    put_gamma: float = 0.0
    put_theta: float = 0.0
    put_vega: float = 0.0
    put_oi: int = 0
    put_volume: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionsChainSnapshot:
    underlying: str
    underlying_price: float
    timestamp: datetime
    expiry: date
    strikes: list[StrikeData]
    pcr_oi: float = 0.0
    pcr_volume: float = 0.0
    atm_iv: float = 0.0
    iv_rank: float = 0.0
    iv_percentile: float = 0.0

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "underlying_price": self.underlying_price,
            "timestamp": self.timestamp.isoformat(),
            "expiry": self.expiry.isoformat(),
            "strikes": [s.to_dict() for s in self.strikes],
            "pcr_oi": self.pcr_oi,
            "pcr_volume": self.pcr_volume,
            "atm_iv": self.atm_iv,
            "iv_rank": self.iv_rank,
            "iv_percentile": self.iv_percentile,
        }


# ---------------------------------------------------------------------------
# Internal mutable state per underlying+expiry
# ---------------------------------------------------------------------------

@dataclass
class _StrikeState:
    """Mutable state for a single strike, accumulated from ticks."""
    strike: float
    call_ltp: float = 0.0
    call_oi: int = 0
    call_volume: int = 0
    call_bid: float = 0.0
    call_ask: float = 0.0
    put_ltp: float = 0.0
    put_oi: int = 0
    put_volume: int = 0
    put_bid: float = 0.0
    put_ask: float = 0.0


@dataclass
class _ChainState:
    """Mutable state for one underlying + expiry combination."""
    underlying: str
    segment: str
    expiry: date
    underlying_price: float = 0.0
    strikes: dict[float, _StrikeState] = field(default_factory=dict)
    last_updated: float = 0.0  # monotonic timestamp


class ChainProcessor:
    """Maintains in-memory chain state, computes and publishes snapshots."""

    def __init__(self, db=None):
        # {(underlying, expiry_str): _ChainState}
        self._chains: dict[tuple[str, str], _ChainState] = {}
        self._db = db
        # Cache of IV rank values {underlying: (iv_rank, iv_percentile, last_calc_ts)}
        self._iv_rank_cache: dict[str, tuple[float, float, float]] = {}
        self._iv_rank_ttl = 60.0  # refresh IV rank every 60 s

    # ------------------------------------------------------------------
    # Tick ingestion
    # ------------------------------------------------------------------

    def process_tick(self, tick: dict) -> None:
        """Ingest a single tick and update internal chain state.

        ``tick`` should be a dict with the canonical Tick fields.
        """
        symbol = tick.get("symbol", "")
        segment = tick.get("segment", "")
        option_type = tick.get("option_type")  # "CE" | "PE" | None
        strike = tick.get("strike")
        expiry_raw = tick.get("expiry")
        underlying_price = tick.get("underlying_price")
        last_price = tick.get("last_price", 0.0)

        # Determine the underlying from the symbol
        # For index ticks (no option_type), the symbol IS the underlying
        underlying = symbol

        # Determine expiry — for index/spot ticks there is no expiry
        if option_type is None or strike is None or expiry_raw is None:
            # This is a spot/index tick — update underlying_price on all matching chains
            for key, chain in self._chains.items():
                if key[0] == symbol:
                    chain.underlying_price = last_price
                    chain.last_updated = time.monotonic()
            return

        # Parse expiry
        if isinstance(expiry_raw, str):
            expiry = date.fromisoformat(expiry_raw)
        elif isinstance(expiry_raw, date):
            expiry = expiry_raw
        else:
            return

        key = (underlying, expiry.isoformat())

        if key not in self._chains:
            self._chains[key] = _ChainState(
                underlying=underlying,
                segment=segment,
                expiry=expiry,
            )

        chain = self._chains[key]

        if underlying_price is not None:
            chain.underlying_price = underlying_price

        chain.last_updated = time.monotonic()

        # Upsert strike state
        if strike not in chain.strikes:
            chain.strikes[strike] = _StrikeState(strike=strike)

        ss = chain.strikes[strike]

        if option_type == "CE":
            ss.call_ltp = last_price
            ss.call_oi = tick.get("oi", ss.call_oi)
            ss.call_volume = tick.get("volume", ss.call_volume)
            ss.call_bid = tick.get("bid", ss.call_bid)
            ss.call_ask = tick.get("ask", ss.call_ask)
        elif option_type == "PE":
            ss.put_ltp = last_price
            ss.put_oi = tick.get("oi", ss.put_oi)
            ss.put_volume = tick.get("volume", ss.put_volume)
            ss.put_bid = tick.get("bid", ss.put_bid)
            ss.put_ask = tick.get("ask", ss.put_ask)

    # ------------------------------------------------------------------
    # Snapshot building (vectorised Greeks)
    # ------------------------------------------------------------------

    async def build_snapshot(
        self,
        underlying: str,
        expiry: Optional[date] = None,
    ) -> Optional[OptionsChainSnapshot]:
        """Build a full OptionsChainSnapshot for the given underlying.

        If *expiry* is None, uses the nearest expiry available.
        """
        # Find matching chain
        chain = self._find_chain(underlying, expiry)
        if chain is None or not chain.strikes or chain.underlying_price <= 0:
            return None

        start_ts = time.monotonic()

        spot = chain.underlying_price
        sorted_strikes = sorted(chain.strikes.values(), key=lambda s: s.strike)
        n = len(sorted_strikes)

        # Build numpy arrays for vectorised computation
        strike_arr = np.array([s.strike for s in sorted_strikes], dtype=np.float64)
        spot_arr = np.full(n, spot, dtype=np.float64)

        # Time to expiry in years
        now = datetime.now(timezone.utc)
        expiry_dt = datetime.combine(chain.expiry, datetime.min.time()).replace(
            hour=15, minute=30, tzinfo=timezone.utc  # NSE closing time
        )
        tte_seconds = max((expiry_dt - now).total_seconds(), 60.0)
        tte_years = tte_seconds / (365.25 * 24 * 3600)
        T_arr = np.full(n, tte_years, dtype=np.float64)

        call_prices = np.array([s.call_ltp for s in sorted_strikes], dtype=np.float64)
        put_prices = np.array([s.put_ltp for s in sorted_strikes], dtype=np.float64)

        # ── Step 1: Compute IV via Newton-Raphson ────────────────────────
        is_call = np.ones(n, dtype=np.bool_)
        is_put = np.zeros(n, dtype=np.bool_)

        call_iv = newton_raphson_iv(call_prices, spot_arr, strike_arr, T_arr, RISK_FREE_RATE, is_call)
        put_iv = newton_raphson_iv(put_prices, spot_arr, strike_arr, T_arr, RISK_FREE_RATE, is_put)

        # Replace NaN with 0 for failed solves
        call_iv = np.nan_to_num(call_iv, nan=0.0)
        put_iv = np.nan_to_num(put_iv, nan=0.0)

        # ── Step 2: Moneyness filter — skip deep ITM (>15%) ─────────────
        call_type_arr = np.ones(n, dtype=np.int64)
        put_type_arr = np.full(n, -1, dtype=np.int64)
        call_mask = moneyness_mask(spot_arr, strike_arr, call_type_arr)
        put_mask = moneyness_mask(spot_arr, strike_arr, put_type_arr)

        # Zero out IV for deep ITM options
        call_iv = np.where(call_mask, call_iv, 0.0)
        put_iv = np.where(put_mask, put_iv, 0.0)

        # ── Step 3: Compute Greeks using solved IV ───────────────────────
        # Use a small default IV where solver failed to avoid div-by-zero
        call_iv_safe = np.where(call_iv > 0.001, call_iv, 0.20)
        put_iv_safe = np.where(put_iv > 0.001, put_iv, 0.20)

        (
            _, _, call_delta, _, call_gamma, call_theta, _, call_vega
        ) = black_scholes_vectorised(spot_arr, strike_arr, T_arr, RISK_FREE_RATE, call_iv_safe)

        (
            _, _, _, put_delta, put_gamma, _, put_theta, put_vega
        ) = black_scholes_vectorised(spot_arr, strike_arr, T_arr, RISK_FREE_RATE, put_iv_safe)

        # ── Step 4: ATM IV ───────────────────────────────────────────────
        atm_idx = int(np.argmin(np.abs(strike_arr - spot)))
        atm_iv = float((call_iv[atm_idx] + put_iv[atm_idx]) / 2.0)
        if atm_iv <= 0.0:
            atm_iv = float(max(call_iv[atm_idx], put_iv[atm_idx]))

        # ── Step 5: PCR ──────────────────────────────────────────────────
        pcr_input = [
            {
                "call_oi": s.call_oi,
                "put_oi": s.put_oi,
                "call_volume": s.call_volume,
                "put_volume": s.put_volume,
            }
            for s in sorted_strikes
        ]
        pcr_result = calculate_pcr(pcr_input)

        # ── Step 6: IV Rank (cached, async) ──────────────────────────────
        iv_rank = 0.0
        iv_percentile = 0.0
        if self._db is not None:
            cached = self._iv_rank_cache.get(underlying)
            mono_now = time.monotonic()
            if cached is not None and (mono_now - cached[2]) < self._iv_rank_ttl:
                iv_rank, iv_percentile = cached[0], cached[1]
            else:
                try:
                    iv_rank, iv_percentile = await calculate_iv_rank(
                        underlying, atm_iv, self._db
                    )
                    self._iv_rank_cache[underlying] = (iv_rank, iv_percentile, mono_now)
                except Exception as exc:
                    logger.warning("iv_rank_calc_failed", underlying=underlying, error=str(exc))

        # ── Step 7: Assemble StrikeData list ─────────────────────────────
        strike_data_list: list[StrikeData] = []
        for i, ss in enumerate(sorted_strikes):
            sd = StrikeData(
                strike=ss.strike,
                call_ltp=ss.call_ltp,
                call_iv=round(float(call_iv[i]), 6),
                call_delta=round(float(call_delta[i]), 6),
                call_gamma=round(float(call_gamma[i]), 8),
                call_theta=round(float(call_theta[i]), 4),
                call_vega=round(float(call_vega[i]), 4),
                call_oi=ss.call_oi,
                call_volume=ss.call_volume,
                put_ltp=ss.put_ltp,
                put_iv=round(float(put_iv[i]), 6),
                put_delta=round(float(put_delta[i]), 6),
                put_gamma=round(float(put_gamma[i]), 8),
                put_theta=round(float(put_theta[i]), 4),
                put_vega=round(float(put_vega[i]), 4),
                put_oi=ss.put_oi,
                put_volume=ss.put_volume,
            )
            strike_data_list.append(sd)

        elapsed_ms = (time.monotonic() - start_ts) * 1000
        logger.debug(
            "chain_snapshot_built",
            underlying=underlying,
            expiry=chain.expiry.isoformat(),
            num_strikes=n,
            elapsed_ms=round(elapsed_ms, 2),
        )

        return OptionsChainSnapshot(
            underlying=underlying,
            underlying_price=spot,
            timestamp=datetime.now(timezone.utc),
            expiry=chain.expiry,
            strikes=strike_data_list,
            pcr_oi=pcr_result.pcr_oi,
            pcr_volume=pcr_result.pcr_volume,
            atm_iv=round(atm_iv, 6),
            iv_rank=round(iv_rank, 2),
            iv_percentile=round(iv_percentile, 2),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_chain(
        self, underlying: str, expiry: Optional[date] = None
    ) -> Optional[_ChainState]:
        """Find the chain for *underlying*.  Picks nearest expiry if none given."""
        if expiry is not None:
            return self._chains.get((underlying, expiry.isoformat()))

        # Find the nearest expiry for this underlying
        candidates: list[_ChainState] = [
            c for key, c in self._chains.items() if key[0] == underlying
        ]
        if not candidates:
            return None

        today = date.today()
        candidates.sort(key=lambda c: abs((c.expiry - today).days))
        return candidates[0]

    def get_underlyings(self) -> list[str]:
        """Return list of distinct underlyings currently tracked."""
        return list({key[0] for key in self._chains})

    def get_chain_keys(self) -> list[tuple[str, str]]:
        """Return all (underlying, expiry_iso) keys."""
        return list(self._chains.keys())

    def get_segment(self, underlying: str) -> str:
        """Return segment string for a tracked underlying."""
        for key, chain in self._chains.items():
            if key[0] == underlying:
                return chain.segment
        return "NSE_FO"
