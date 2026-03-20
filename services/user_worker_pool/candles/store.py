"""CandleStore — real-time candle building with historical warmup.

On startup:
  1. Fetch today's historical 1m candles from Dhan REST API
  2. Build 5m candles from 1m data
  3. Subscribe to NATS ticks for real-time updates

On each tick:
  1. Update current 1m candle (OHLCV)
  2. When 1m closes, aggregate into 5m
  3. Strategies always have full candle history from market open

No mock data. No synthetic values. Real market data only.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
from typing import Any

import httpx
import numpy as np
import structlog

logger = structlog.get_logger(service="user_worker_pool", module="candle_store")

IST = timezone(timedelta(hours=5, minutes=30))

# Dhan API endpoints
DHAN_CHART_INTRADAY = "https://api.dhan.co/v2/charts/intraday"

# Dhan exchange segment mapping (matches feed_gateway)
# Maps symbol names (as stored in DB) to Dhan API parameters
DHAN_EXCHANGE_MAP = {
    # Indexes
    "NIFTY": ("IDX_I", 13),
    "NIFTY_50": ("IDX_I", 13),    # backtest alias
    "BANKNIFTY": ("IDX_I", 25),
    "BANK_NIFTY": ("IDX_I", 25),  # backtest alias
    "FINNIFTY": ("IDX_I", 27),
    "MIDCPNIFTY": ("IDX_I", 442),
    "SENSEX": ("IDX_I", 51),
    # Shares (DB stores without NSE: prefix)
    "RELIANCE": ("NSE_EQ", 2885),
    "HDFCBANK": ("NSE_EQ", 1333),
    "INFY": ("NSE_EQ", 1594),
    "TCS": ("NSE_EQ", 11536),
    "ICICIBANK": ("NSE_EQ", 4963),
    "SBIN": ("NSE_EQ", 3045),
    # Also support prefixed format from feed_gateway
    "NSE:RELIANCE": ("NSE_EQ", 2885),
    "NSE:HDFCBANK": ("NSE_EQ", 1333),
    "NSE:INFY": ("NSE_EQ", 1594),
    "NSE:TCS": ("NSE_EQ", 11536),
    "NSE:ICICIBANK": ("NSE_EQ", 4963),
    "NSE:SBIN": ("NSE_EQ", 3045),
}

# Max candles to keep in memory per symbol
MAX_1M_BARS = 500
MAX_5M_BARS = 200


class CandleBuffer:
    """Rolling buffer of OHLCV candles for one symbol at one timeframe."""

    def __init__(self, max_bars: int = 500):
        self.max_bars = max_bars
        self.open: list[float] = []
        self.high: list[float] = []
        self.low: list[float] = []
        self.close: list[float] = []
        self.volume: list[float] = []
        self.timestamp: list[float] = []  # Unix timestamps

    def append(self, ts: float, o: float, h: float, l: float, c: float, v: float = 0.0):
        self.timestamp.append(ts)
        self.open.append(o)
        self.high.append(h)
        self.low.append(l)
        self.close.append(c)
        self.volume.append(v)
        # Trim to max
        if len(self.timestamp) > self.max_bars:
            excess = len(self.timestamp) - self.max_bars
            self.timestamp = self.timestamp[excess:]
            self.open = self.open[excess:]
            self.high = self.high[excess:]
            self.low = self.low[excess:]
            self.close = self.close[excess:]
            self.volume = self.volume[excess:]

    def to_dict(self) -> dict:
        """Return as dict of numpy arrays (format strategies expect)."""
        if not self.close:
            return {}
        return {
            "open": np.array(self.open, dtype=np.float64),
            "high": np.array(self.high, dtype=np.float64),
            "low": np.array(self.low, dtype=np.float64),
            "close": np.array(self.close, dtype=np.float64),
            "volume": np.array(self.volume, dtype=np.float64),
            "timestamp": np.array(self.timestamp, dtype=np.float64),
        }

    @property
    def count(self) -> int:
        return len(self.close)

    @property
    def last_close(self) -> float:
        return self.close[-1] if self.close else 0.0


class SymbolCandles:
    """Manages 1m + 5m candle buffers for a single symbol."""

    def __init__(self):
        self.candles_1m = CandleBuffer(max_bars=MAX_1M_BARS)
        self.candles_5m = CandleBuffer(max_bars=MAX_5M_BARS)
        # Current forming candle
        self._cur_1m_ts: float = 0
        self._cur_1m_o: float = 0
        self._cur_1m_h: float = 0
        self._cur_1m_l: float = 0
        self._cur_1m_c: float = 0
        self._cur_1m_v: float = 0
        # Current forming 5m candle
        self._cur_5m_ts: float = 0
        self._cur_5m_o: float = 0
        self._cur_5m_h: float = 0
        self._cur_5m_l: float = 0
        self._cur_5m_c: float = 0
        self._cur_5m_v: float = 0

    def on_tick(self, price: float, volume: float, tick_ts: float):
        """Process a new tick — update current 1m and 5m candles."""
        if price <= 0:
            return

        # Determine 1m period
        period_1m = int(tick_ts // 60) * 60

        if period_1m != self._cur_1m_ts:
            # New 1m candle — close previous if exists
            if self._cur_1m_ts > 0:
                self.candles_1m.append(
                    self._cur_1m_ts, self._cur_1m_o,
                    self._cur_1m_h, self._cur_1m_l, self._cur_1m_c, self._cur_1m_v,
                )
            # Start new 1m candle
            self._cur_1m_ts = period_1m
            self._cur_1m_o = price
            self._cur_1m_h = price
            self._cur_1m_l = price
            self._cur_1m_c = price
            self._cur_1m_v = volume
        else:
            # Update current 1m candle
            if price > self._cur_1m_h:
                self._cur_1m_h = price
            if price < self._cur_1m_l:
                self._cur_1m_l = price
            self._cur_1m_c = price
            self._cur_1m_v += volume

        # Determine 5m period
        period_5m = int(tick_ts // 300) * 300

        if period_5m != self._cur_5m_ts:
            # New 5m candle — close previous if exists
            if self._cur_5m_ts > 0:
                self.candles_5m.append(
                    self._cur_5m_ts, self._cur_5m_o,
                    self._cur_5m_h, self._cur_5m_l, self._cur_5m_c, self._cur_5m_v,
                )
            # Start new 5m candle
            self._cur_5m_ts = period_5m
            self._cur_5m_o = price
            self._cur_5m_h = price
            self._cur_5m_l = price
            self._cur_5m_c = price
            self._cur_5m_v = volume
        else:
            # Update current 5m candle
            if price > self._cur_5m_h:
                self._cur_5m_h = price
            if price < self._cur_5m_l:
                self._cur_5m_l = price
            self._cur_5m_c = price
            self._cur_5m_v += volume

    def get_candles_1m(self) -> dict:
        """Get 1m candles including the current forming bar."""
        d = self.candles_1m.to_dict()
        if self._cur_1m_ts > 0:
            # Append current forming candle
            for k, v in [
                ("timestamp", self._cur_1m_ts), ("open", self._cur_1m_o),
                ("high", self._cur_1m_h), ("low", self._cur_1m_l),
                ("close", self._cur_1m_c), ("volume", self._cur_1m_v),
            ]:
                if k in d:
                    d[k] = np.append(d[k], v)
                else:
                    d[k] = np.array([v], dtype=np.float64)
        return d

    def get_candles_5m(self) -> dict:
        """Get 5m candles including the current forming bar."""
        d = self.candles_5m.to_dict()
        if self._cur_5m_ts > 0:
            for k, v in [
                ("timestamp", self._cur_5m_ts), ("open", self._cur_5m_o),
                ("high", self._cur_5m_h), ("low", self._cur_5m_l),
                ("close", self._cur_5m_c), ("volume", self._cur_5m_v),
            ]:
                if k in d:
                    d[k] = np.append(d[k], v)
                else:
                    d[k] = np.array([v], dtype=np.float64)
        return d


class CandleStore:
    """Manages candle data for all symbols.

    Usage:
        store = CandleStore()
        await store.warmup(["NIFTY_50", "BANK_NIFTY"])  # fetch today's history
        store.on_tick("NIFTY_50", 24350.5, 100, time.time())  # live updates
        candles_5m = store.get_candles("NIFTY_50", "5m")  # for strategy eval
    """

    def __init__(self):
        self._symbols: dict[str, SymbolCandles] = defaultdict(SymbolCandles)
        self._access_token = os.environ.get("FEED_ACCESS_TOKEN", "")
        self._client_id = os.environ.get("FEED_CLIENT_ID", "")
        self._warmed_up: set[str] = set()
        self._last_tick_time: dict[str, float] = {}  # symbol → last tick unix ts
        self._polling_active = False
        self._poll_symbols: list[str] = []  # symbols to poll when WS is down

    def on_tick(self, symbol: str, price: float, volume: float = 0.0, tick_ts: float | None = None):
        """Process a live tick for a symbol."""
        ts = tick_ts or time.time()
        self._symbols[symbol].on_tick(price, volume, ts)
        self._last_tick_time[symbol] = time.time()  # wall clock, not tick ts

    def has_data(self, symbol: str) -> bool:
        """Check if we have any candle data for this symbol."""
        sc = self._symbols.get(symbol)
        return sc is not None and sc.candles_1m.count > 0

    def is_tick_stale(self, symbol: str, stale_seconds: float = 90) -> bool:
        """True if no tick received for this symbol in stale_seconds."""
        last = self._last_tick_time.get(symbol, 0)
        return (time.time() - last) > stale_seconds

    def set_poll_symbols(self, symbols: list[str]):
        """Set the list of symbols to poll when WebSocket is down."""
        self._poll_symbols = symbols

    async def run_fallback_poller(self):
        """Background task: poll Dhan REST API for 1m candles when ticks are stale.

        Runs continuously. When ticks are flowing (WebSocket healthy), does nothing.
        When ticks stop (WebSocket 429/disconnect), fetches latest candle every minute.
        """
        if not self._access_token:
            return

        logger.info("fallback_poller_started", symbols=self._poll_symbols)

        while True:
            await asyncio.sleep(15)  # check every 15 seconds

            # Only during market hours (9:15-15:30 IST = 3:45-10:00 UTC)
            now = datetime.now(IST)
            if now.hour < 9 or (now.hour == 9 and now.minute < 15) or now.hour >= 16:
                continue

            # Check which symbols have stale ticks
            stale_symbols = [s for s in self._poll_symbols if self.is_tick_stale(s)]

            if not stale_symbols:
                if self._polling_active:
                    logger.info("fallback_poller_ticks_resumed")
                    self._polling_active = False
                continue

            if not self._polling_active:
                logger.warning("fallback_poller_activated",
                               stale_symbols=stale_symbols,
                               reason="no ticks for 90+ seconds")
                self._polling_active = True

            # Poll latest 1m candle for each stale symbol
            for symbol in stale_symbols:
                try:
                    await self._poll_latest_candle(symbol)
                except Exception as exc:
                    logger.warning("fallback_poll_error", symbol=symbol, error=str(exc))
                await asyncio.sleep(0.3)  # rate limit

    async def _poll_latest_candle(self, symbol: str):
        """Fetch today's latest candles from Dhan REST API (fallback)."""
        lookup = DHAN_EXCHANGE_MAP.get(symbol)
        if lookup is None:
            return

        exchange_segment, security_id = lookup
        today = datetime.now(IST).date()
        instrument_type = "INDEX" if exchange_segment == "IDX_I" else "EQUITY"

        headers = {
            "Content-Type": "application/json",
            "access-token": self._access_token,
            "client-id": self._client_id,
        }
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument_type,
            "interval": "1",
            "fromDate": today.isoformat(),
            "toDate": today.isoformat(),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(DHAN_CHART_INTRADAY, json=payload, headers=headers)

        if resp.status_code != 200:
            return

        data = resp.json()
        closes = data.get("close", [])
        timestamps = data.get("timestamp", [])

        if not closes or not timestamps:
            return

        # Only ingest the LAST candle (most recent 1m bar)
        sc = self._symbols[symbol]
        last_idx = len(closes) - 1
        ts = float(timestamps[last_idx])
        o = float(data.get("open", closes)[last_idx])
        h = float(data.get("high", closes)[last_idx])
        l = float(data.get("low", closes)[last_idx])
        c = float(closes[last_idx])
        v = float(data.get("volume", [0] * len(closes))[last_idx])

        # Only append if this is a new candle we haven't seen
        if sc.candles_1m.timestamp and ts <= sc.candles_1m.timestamp[-1]:
            return  # already have this candle

        sc.on_tick(c, v, ts)  # simulates a tick at the close price
        self._last_tick_time[symbol] = time.time()

        logger.debug("fallback_candle_fetched", symbol=symbol, close=c, ts=ts)

    def get_candles(self, symbol: str, timeframe: str = "5m") -> dict:
        """Get candle data for a symbol. Returns dict of numpy arrays or empty dict."""
        sc = self._symbols.get(symbol)
        if sc is None:
            return {}
        if timeframe == "1m":
            return sc.get_candles_1m()
        return sc.get_candles_5m()

    def get_bar_count(self, symbol: str, timeframe: str = "5m") -> int:
        sc = self._symbols.get(symbol)
        if sc is None:
            return 0
        buf = sc.candles_5m if timeframe == "5m" else sc.candles_1m
        return buf.count

    def is_warmed_up(self, symbol: str) -> bool:
        return symbol in self._warmed_up

    async def warmup(self, symbols: list[str]):
        """Fetch today's historical candles from Dhan API for all symbols."""
        if not self._access_token:
            logger.warning("candle_warmup_skip", reason="no FEED_ACCESS_TOKEN")
            return

        for i, sym in enumerate(symbols):
            try:
                await self._warmup_symbol(sym)
            except Exception as exc:
                logger.error("candle_warmup_error", symbol=sym, error=str(exc))
            if i < len(symbols) - 1:
                await asyncio.sleep(0.3)  # avoid Dhan rate limit

    async def _resolve_security_id(self, symbol: str) -> tuple[str, int] | None:
        """Resolve a symbol to (exchange_segment, security_id) using Dhan scrip master."""
        # Check hardcoded map first
        lookup = DHAN_EXCHANGE_MAP.get(symbol)
        if lookup:
            return lookup
        alt = symbol.replace("_", "").replace(" ", "")
        lookup = DHAN_EXCHANGE_MAP.get(alt)
        if lookup:
            return lookup

        # Try Dhan compact market data API to resolve unknown symbols
        # Assume equity if not in index map
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
        if symbol in indices:
            return None  # should be in hardcoded map

        # Search Dhan scrip master for security_id
        try:
            headers = {"Content-Type": "application/json", "access-token": self._access_token, "client-id": self._client_id}
            search_url = f"https://api.dhan.co/v2/searchscrip?query={symbol}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(search_url, headers=headers)
                if resp.status_code == 200:
                    results = resp.json()
                    if isinstance(results, list):
                        for r in results:
                            if r.get("symbol", "").upper() == symbol.upper() and r.get("exchange") == "NSE":
                                sec_id = int(r["securityId"])
                                DHAN_EXCHANGE_MAP[symbol] = ("NSE_EQ", sec_id)
                                logger.info("symbol_resolved", symbol=symbol, security_id=sec_id)
                                return ("NSE_EQ", sec_id)
                    elif isinstance(results, dict) and results.get("data"):
                        for r in results["data"]:
                            if r.get("tradingSymbol", "").upper() == symbol.upper():
                                sec_id = int(r["securityId"])
                                DHAN_EXCHANGE_MAP[symbol] = ("NSE_EQ", sec_id)
                                logger.info("symbol_resolved", symbol=symbol, security_id=sec_id)
                                return ("NSE_EQ", sec_id)
        except Exception as exc:
            logger.warning("symbol_resolve_error", symbol=symbol, error=str(exc))

        logger.warning("candle_warmup_no_mapping", symbol=symbol)
        return None

    async def _warmup_symbol(self, symbol: str):
        """Fetch today's 1m candles for one symbol from Dhan REST API."""
        resolved = await self._resolve_security_id(symbol)
        if resolved is None:
            return

        exchange_segment, security_id = resolved
        today = datetime.now(IST).date()

        instrument_type = "INDEX" if exchange_segment == "IDX_I" else "EQUITY"

        headers = {
            "Content-Type": "application/json",
            "access-token": self._access_token,
            "client-id": self._client_id,
        }

        sc = self._symbols[symbol]

        # Load last 3 trading days (previous days first, then today)
        # This ensures EMA(33) etc. have enough history even at 9:15 AM
        loaded_days = 0
        for days_back in range(7, -1, -1):  # 7 days back to today (covers weekends+holidays)
            if loaded_days >= 3:
                break
            attempt_date = today - timedelta(days=days_back)
            if attempt_date > today:
                continue

            payload = {
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument_type,
                "interval": "1",
                "fromDate": attempt_date.isoformat(),
                "toDate": attempt_date.isoformat(),
            }

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(DHAN_CHART_INTRADAY, json=payload, headers=headers)

                    if resp.status_code == 200:
                        candidate = resp.json()
                        if candidate.get("close") and len(candidate["close"]) > 0:
                            self._ingest_candle_data(sc, candidate)
                            loaded_days += 1
                            logger.info("candle_warmup_day", symbol=symbol,
                                        date=str(attempt_date), bars=len(candidate["close"]))
                await asyncio.sleep(0.2)  # rate limit
            except Exception:
                continue

        if loaded_days == 0:
            logger.warning("candle_warmup_no_data", symbol=symbol)
            return

        self._warmed_up.add(symbol)

        # Also make data available under aliases (NIFTY_50↔NIFTY, BANK_NIFTY↔BANKNIFTY)
        aliases = {
            "NIFTY": "NIFTY_50", "NIFTY_50": "NIFTY",
            "BANKNIFTY": "BANK_NIFTY", "BANK_NIFTY": "BANKNIFTY",
        }
        alias = aliases.get(symbol)
        if alias and alias not in self._symbols:
            self._symbols[alias] = sc  # share the same buffer
            self._warmed_up.add(alias)

        logger.info(
            "candle_warmup_done",
            symbol=symbol,
            days_loaded=loaded_days,
            bars_1m=sc.candles_1m.count,
            bars_5m=sc.candles_5m.count,
        )

    def _ingest_candle_data(self, sc: SymbolCandles, data: dict):
        """Ingest a day's worth of candle data into a SymbolCandles buffer."""
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])
        timestamps = data.get("timestamp", [])

        if not closes or not timestamps:
            return

        for i in range(len(closes)):
            ts = float(timestamps[i])
            o = float(opens[i]) if i < len(opens) else float(closes[i])
            h = float(highs[i]) if i < len(highs) else o
            l = float(lows[i]) if i < len(lows) else o
            c = float(closes[i])
            v = float(volumes[i]) if i < len(volumes) else 0.0

            sc.candles_1m.append(ts, o, h, l, c, v)

            period_5m = int(ts // 300) * 300
            if period_5m != sc._cur_5m_ts:
                if sc._cur_5m_ts > 0:
                    sc.candles_5m.append(
                        sc._cur_5m_ts, sc._cur_5m_o,
                        sc._cur_5m_h, sc._cur_5m_l, sc._cur_5m_c, sc._cur_5m_v,
                    )
                sc._cur_5m_ts = period_5m
                sc._cur_5m_o = o
                sc._cur_5m_h = h
                sc._cur_5m_l = l
                sc._cur_5m_c = c
                sc._cur_5m_v = v
            else:
                if h > sc._cur_5m_h:
                    sc._cur_5m_h = h
                if l < sc._cur_5m_l:
                    sc._cur_5m_l = l
                sc._cur_5m_c = c
                sc._cur_5m_v += v
