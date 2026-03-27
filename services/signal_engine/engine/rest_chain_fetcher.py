"""
REST-based option chain fetcher using Dhan API.

Downloads the scrip master CSV to discover option contract security IDs,
then uses /v2/marketfeed/quote to fetch live quotes in bulk.
Builds OptionsChainSnapshot compatible with the existing chain pipeline.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta

import aiohttp
import numpy as np
import structlog

from .chain_processor import OptionsChainSnapshot, StrikeData
from .iv_calculator import newton_raphson_iv

logger = structlog.get_logger(service="signal_engine", module="rest_chain_fetcher")

DHAN_BASE_URL = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
RISK_FREE_RATE = 0.065

# Underlyings to track with their Dhan security IDs for spot
UNDERLYING_CONFIG = {
    "NIFTY":      {"spot_sec_id": 13,  "exchange": "IDX_I",  "step": 50,  "segment": "NSE_FO"},
    "BANKNIFTY":  {"spot_sec_id": 25,  "exchange": "IDX_I",  "step": 100, "segment": "NSE_FO"},
    "FINNIFTY":   {"spot_sec_id": 27,  "exchange": "IDX_I",  "step": 50,  "segment": "NSE_FO"},
    "MIDCPNIFTY": {"spot_sec_id": 442, "exchange": "IDX_I",  "step": 25,  "segment": "NSE_FO"},
    "SENSEX":     {"spot_sec_id": 51,  "exchange": "IDX_I",  "step": 100, "segment": "BSE_FO"},
}

MAX_STRIKES_EACH_SIDE = 15  # ±15 strikes around ATM


@dataclass
class OptionContract:
    security_id: int
    symbol: str
    underlying: str
    strike: float
    option_type: str  # "CE" or "PE"
    expiry: date
    exchange: str  # "NSE_FNO" or "BSE_FNO"


class RestChainFetcher:
    """Fetches option chain data via Dhan REST API."""

    def __init__(self):
        self._access_token = os.environ.get("FEED_ACCESS_TOKEN", "")
        self._client_id = os.environ.get("FEED_CLIENT_ID", "")
        self._session: aiohttp.ClientSession | None = None
        # underlying -> list[OptionContract] for nearest expiry
        self._contracts: dict[str, list[OptionContract]] = {}
        # underlying -> spot price
        self._spot_prices: dict[str, float] = {}
        self._last_scrip_sync: float = 0
        self._scrip_sync_interval = 3600  # re-sync scrip master every hour

    async def start(self) -> None:
        if not self._access_token:
            logger.warning("rest_chain_fetcher_disabled", reason="no FEED_ACCESS_TOKEN")
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                "access-token": self._access_token,
                "client-id": self._client_id,
                "Content-Type": "application/json",
            },
        )
        await self._sync_scrip_master()
        logger.info("rest_chain_fetcher_started", underlyings=list(self._contracts.keys()))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def fetch_chains(self) -> list[OptionsChainSnapshot]:
        """Fetch option chains for all configured underlyings. Returns list of snapshots."""
        if not self._session or not self._access_token:
            return []

        # Re-sync scrip master periodically (expiry changes daily)
        if time.time() - self._last_scrip_sync > self._scrip_sync_interval:
            await self._sync_scrip_master()

        # First fetch spot prices for all underlyings
        await self._fetch_spot_prices()

        snapshots = []
        for underlying, contracts in self._contracts.items():
            if not contracts:
                continue
            spot = self._spot_prices.get(underlying, 0.0)
            if spot <= 0:
                continue

            try:
                snapshot = await self._fetch_chain_for_underlying(underlying, contracts, spot)
                if snapshot and len(snapshot.strikes) > 0:
                    snapshots.append(snapshot)
            except Exception as exc:
                logger.error("chain_fetch_error", underlying=underlying, error=str(exc))

            await asyncio.sleep(1.0)  # Stagger between underlyings

        return snapshots

    # ------------------------------------------------------------------
    # Scrip master parsing
    # ------------------------------------------------------------------

    async def _sync_scrip_master(self) -> None:
        """Download and parse Dhan scrip master to find option contracts."""
        try:
            logger.info("scrip_master_downloading")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as sess:
                async with sess.get(SCRIP_MASTER_URL) as resp:
                    if resp.status != 200:
                        logger.error("scrip_master_download_failed", status=resp.status)
                        return
                    text = await resp.text()

            self._parse_scrip_master(text)
            self._last_scrip_sync = time.time()
        except Exception as exc:
            logger.error("scrip_master_sync_error", error=str(exc))

    def _parse_scrip_master(self, csv_text: str) -> None:
        """Parse CSV to find option contracts for each underlying's nearest expiry."""
        reader = csv.DictReader(io.StringIO(csv_text))

        # Collect all option contracts grouped by underlying
        all_contracts: dict[str, list[OptionContract]] = defaultdict(list)

        for row in reader:
            try:
                exchange = row.get("SEM_EXM_EXCH_ID", "").strip()
                instrument = row.get("SEM_INSTRUMENT_NAME", "").strip()
                symbol = row.get("SEM_TRADING_SYMBOL", "").strip()
                sec_id_str = row.get("SEM_SMST_SECURITY_ID", "").strip()
                expiry_str = row.get("SEM_EXPIRY_DATE", "").strip()
                option_type = row.get("SEM_OPTION_TYPE", "").strip()
                strike_str = row.get("SEM_STRIKE_PRICE", "").strip()
                custom_symbol = row.get("SEM_CUSTOM_SYMBOL", "").strip()

                # Only index options (OPTIDX) and stock options (OPTSTK) on NSE/BSE
                if instrument not in ("OPTIDX", "OPTSTK"):
                    continue
                if exchange not in ("NSE", "BSE"):
                    continue
                if not sec_id_str or not expiry_str or not option_type or not strike_str:
                    continue

                # Determine underlying from custom_symbol or trading_symbol
                # Custom symbol format: "NIFTY 27 MAR 2025 CE 23000" or "NIFTY-Mar2025-23000-CE"
                underlying = None
                for u in UNDERLYING_CONFIG:
                    if symbol.startswith(u) or custom_symbol.startswith(u):
                        underlying = u
                        break
                if not underlying:
                    continue

                sec_id = int(sec_id_str)
                strike = float(strike_str)
                expiry = _parse_expiry(expiry_str)
                if expiry is None or expiry < date.today():
                    continue

                ot = "CE" if option_type == "CE" else "PE"
                exch_seg = "NSE_FNO" if exchange == "NSE" else "BSE_FNO"

                all_contracts[underlying].append(OptionContract(
                    security_id=sec_id,
                    symbol=symbol,
                    underlying=underlying,
                    strike=strike,
                    option_type=ot,
                    expiry=expiry,
                    exchange=exch_seg,
                ))
            except (ValueError, KeyError):
                continue

        # For each underlying, pick the nearest expiry and filter strikes around ATM
        for underlying, contracts in all_contracts.items():
            if not contracts:
                continue

            # Find nearest expiry
            today = date.today()
            expiries = sorted(set(c.expiry for c in contracts))
            # Pick first expiry that is today or later
            nearest = None
            for exp in expiries:
                if exp >= today:
                    nearest = exp
                    break
            if nearest is None:
                continue

            # Filter to nearest expiry only
            nearest_contracts = [c for c in contracts if c.expiry == nearest]

            self._contracts[underlying] = nearest_contracts
            logger.info(
                "scrip_master_parsed",
                underlying=underlying,
                expiry=nearest.isoformat(),
                contracts=len(nearest_contracts),
            )

    # ------------------------------------------------------------------
    # Quote fetching
    # ------------------------------------------------------------------

    async def _fetch_spot_prices(self) -> None:
        """Fetch spot/index prices for all underlyings."""
        # Build request: IDX_I security IDs
        idx_ids = []
        for underlying, cfg in UNDERLYING_CONFIG.items():
            idx_ids.append(cfg["spot_sec_id"])

        if not idx_ids:
            return

        try:
            payload = {"IDX_I": idx_ids}
            async with self._session.post(f"{DHAN_BASE_URL}/marketfeed/ltp", json=payload) as resp:
                if resp.status != 200:
                    logger.warning("spot_fetch_failed", status=resp.status)
                    return
                data = await resp.json()

            if data.get("status") != "success":
                return

            idx_data = data.get("data", {}).get("IDX_I", {})
            for underlying, cfg in UNDERLYING_CONFIG.items():
                sec_id = str(cfg["spot_sec_id"])
                if sec_id in idx_data:
                    self._spot_prices[underlying] = idx_data[sec_id].get("last_price", 0.0)
        except Exception as exc:
            logger.error("spot_fetch_error", error=str(exc))

    async def _fetch_chain_for_underlying(
        self, underlying: str, contracts: list[OptionContract], spot: float
    ) -> OptionsChainSnapshot | None:
        """Fetch quotes for option contracts and build a chain snapshot."""
        if not contracts:
            return None

        cfg = UNDERLYING_CONFIG.get(underlying)
        if not cfg:
            return None

        step = cfg["step"]
        atm_strike = round(spot / step) * step

        # Filter contracts to ±MAX_STRIKES_EACH_SIDE around ATM
        min_strike = atm_strike - MAX_STRIKES_EACH_SIDE * step
        max_strike = atm_strike + MAX_STRIKES_EACH_SIDE * step
        filtered = [c for c in contracts if min_strike <= c.strike <= max_strike]

        if not filtered:
            return None

        expiry = filtered[0].expiry

        # Fetch quotes in batch (max 1000 per call)
        exchange_key = "NSE_FNO" if filtered[0].exchange == "NSE_FNO" else "BSE_FNO"
        sec_ids = [c.security_id for c in filtered]

        quotes = {}
        for i in range(0, len(sec_ids), 1000):
            batch = sec_ids[i:i + 1000]
            try:
                payload = {exchange_key: batch}
                async with self._session.post(f"{DHAN_BASE_URL}/marketfeed/quote", json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("quote_fetch_failed", status=resp.status, body=body[:200])
                        continue
                    data = await resp.json()

                if data.get("status") == "success":
                    for seg_key, seg_data in data.get("data", {}).items():
                        for sid, quote in seg_data.items():
                            quotes[int(sid)] = quote
            except Exception as exc:
                logger.error("quote_batch_error", underlying=underlying, error=str(exc))

            if i + 1000 < len(sec_ids):
                await asyncio.sleep(2.0)  # Rate limit between batches

        # Build strike map: strike -> {ce_quote, pe_quote}
        strike_map: dict[float, dict] = defaultdict(lambda: {"ce": None, "pe": None})
        for contract in filtered:
            quote = quotes.get(contract.security_id)
            if quote is None:
                continue
            key = "ce" if contract.option_type == "CE" else "pe"
            strike_map[contract.strike][key] = quote

        # Build StrikeData list
        now = datetime.now(timezone.utc)
        tte = max((expiry - date.today()).days / 365.0, 1 / 365.0)

        strikes_list: list[StrikeData] = []
        total_call_oi = 0
        total_put_oi = 0
        total_call_vol = 0
        total_put_vol = 0

        # First pass: collect raw data
        for strike_val in sorted(strike_map.keys()):
            data = strike_map[strike_val]
            ce = data["ce"]
            pe = data["pe"]

            sd = StrikeData(strike=strike_val)

            if ce:
                sd.call_ltp = ce.get("last_price", 0.0)
                sd.call_oi = int(ce.get("oi", 0))
                sd.call_volume = int(ce.get("volume", 0))
                total_call_oi += sd.call_oi
                total_call_vol += sd.call_volume

            if pe:
                sd.put_ltp = pe.get("last_price", 0.0)
                sd.put_oi = int(pe.get("oi", 0))
                sd.put_volume = int(pe.get("volume", 0))
                total_put_oi += sd.put_oi
                total_put_vol += sd.put_volume

            strikes_list.append(sd)

        # Vectorized IV computation
        if strikes_list and spot > 0:
            iv_prices, iv_spots, iv_strikes, iv_ttes, iv_is_call, iv_indices = [], [], [], [], [], []
            for i, sd in enumerate(strikes_list):
                if sd.call_ltp > 0.01:
                    iv_prices.append(sd.call_ltp)
                    iv_spots.append(spot)
                    iv_strikes.append(sd.strike)
                    iv_ttes.append(tte)
                    iv_is_call.append(True)
                    iv_indices.append((i, "call"))
                if sd.put_ltp > 0.01:
                    iv_prices.append(sd.put_ltp)
                    iv_spots.append(spot)
                    iv_strikes.append(sd.strike)
                    iv_ttes.append(tte)
                    iv_is_call.append(False)
                    iv_indices.append((i, "put"))

            if iv_prices:
                try:
                    ivs = newton_raphson_iv(
                        np.array(iv_prices, dtype=np.float64),
                        np.array(iv_spots, dtype=np.float64),
                        np.array(iv_strikes, dtype=np.float64),
                        np.array(iv_ttes, dtype=np.float64),
                        RISK_FREE_RATE,
                        np.array(iv_is_call, dtype=np.bool_),
                    )
                    for k, (idx, opt_type) in enumerate(iv_indices):
                        iv_val = float(ivs[k])
                        if not np.isnan(iv_val) and 0 < iv_val < 5.0:
                            if opt_type == "call":
                                strikes_list[idx].call_iv = round(iv_val, 6)
                            else:
                                strikes_list[idx].put_iv = round(iv_val, 6)
                except Exception as exc:
                    logger.warning("iv_compute_error", underlying=underlying, error=str(exc))

        if not strikes_list:
            return None

        # PCR
        pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0
        pcr_volume = total_put_vol / total_call_vol if total_call_vol > 0 else 0.0

        # ATM IV (average of CE and PE IV at ATM strike)
        atm_iv = 0.0
        for sd in strikes_list:
            if sd.strike == atm_strike:
                ivs = [v for v in (sd.call_iv, sd.put_iv) if v > 0]
                atm_iv = sum(ivs) / len(ivs) if ivs else 0.0
                break

        return OptionsChainSnapshot(
            underlying=underlying,
            underlying_price=spot,
            timestamp=now,
            expiry=expiry,
            strikes=strikes_list,
            pcr_oi=round(pcr_oi, 4),
            pcr_volume=round(pcr_volume, 4),
            atm_iv=round(atm_iv, 6),
            iv_rank=0.0,  # Would need historical data
            iv_percentile=0.0,
        )


def _parse_expiry(expiry_str: str) -> date | None:
    """Parse Dhan expiry date string. Formats: '2026-03-30 14:30:00' or '2025-03-27'."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(expiry_str.strip(), fmt).date()
        except ValueError:
            continue
    return None
