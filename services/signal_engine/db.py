"""
Async database client for signal_engine.

Handles:
  - Writing ATM IV snapshots every 60 s per underlying
  - Reading 52-week IV history on startup
  - Health checks
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import structlog

logger = structlog.get_logger(service="signal_engine", module="db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/resolute",
)

# Parse asyncpg-compatible DSN (strip +asyncpg if present from SQLAlchemy-style URL)
_DB_DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class SignalEngineDB:
    """Async PostgreSQL/TimescaleDB client for IV persistence."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._iv_write_task: Optional[asyncio.Task] = None
        self._shutting_down = False
        # Callable that returns snapshots — set by main after wiring
        self._snapshot_provider = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create asyncpg connection pool."""
        self._pool = await asyncpg.create_pool(
            _DB_DSN,
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
        logger.info("db_connected", dsn=_DB_DSN.split("@")[-1])  # log host only

        # Ensure table exists (idempotent)
        await self._ensure_tables()

    async def close(self) -> None:
        """Shut down the pool and cancel background tasks."""
        self._shutting_down = True

        if self._iv_write_task and not self._iv_write_task.done():
            self._iv_write_task.cancel()
            try:
                await self._iv_write_task
            except asyncio.CancelledError:
                pass

        if self._pool:
            await self._pool.close()
            logger.info("db_pool_closed")

    async def _ensure_tables(self) -> None:
        """Create the atm_iv_snapshots table if it does not exist."""
        ddl = """
        CREATE TABLE IF NOT EXISTS atm_iv_snapshots (
            id            BIGSERIAL,
            symbol        TEXT NOT NULL,
            timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            atm_iv        DOUBLE PRECISION NOT NULL,
            iv_rank       DOUBLE PRECISION,
            iv_percentile DOUBLE PRECISION,
            pcr_oi        DOUBLE PRECISION,
            pcr_volume    DOUBLE PRECISION,
            underlying_price DOUBLE PRECISION,
            PRIMARY KEY (id, timestamp)
        );

        -- TimescaleDB hypertable (no-op if already converted or extension absent)
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable(
                    'atm_iv_snapshots', 'timestamp',
                    if_not_exists => TRUE
                );
            END IF;
        END
        $$;

        CREATE INDEX IF NOT EXISTS idx_atm_iv_symbol_ts
            ON atm_iv_snapshots (symbol, timestamp DESC);
        """
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)
        logger.info("db_tables_ensured")

    # ------------------------------------------------------------------
    # 52-week IV history
    # ------------------------------------------------------------------

    async def get_52_week_iv_history(self, symbol: str) -> list[dict]:
        """Fetch daily ATM IV values for the past 52 weeks.

        Returns list of {"timestamp": ..., "atm_iv": ...} dicts, oldest first.
        """
        if self._pool is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(weeks=52)

        query = """
        SELECT timestamp, atm_iv
        FROM atm_iv_snapshots
        WHERE symbol = $1
          AND timestamp >= $2
        ORDER BY timestamp ASC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, symbol, cutoff)

        return [{"timestamp": row["timestamp"], "atm_iv": float(row["atm_iv"])} for row in rows]

    # ------------------------------------------------------------------
    # Periodic IV snapshot writer
    # ------------------------------------------------------------------

    def start_iv_writer(self, snapshot_provider) -> None:
        """Start background task that writes ATM IV snapshots every 60 s.

        ``snapshot_provider`` is an async callable(underlying) -> OptionsChainSnapshot | None.
        """
        self._snapshot_provider = snapshot_provider
        self._iv_write_task = asyncio.create_task(self._iv_write_loop())

    async def _iv_write_loop(self) -> None:
        """Periodically persist ATM IV for every tracked underlying."""
        logger.info("iv_write_loop_started", interval_s=60)

        while not self._shutting_down:
            try:
                await asyncio.sleep(60)

                if self._snapshot_provider is None:
                    continue

                # The provider also returns the list of underlyings
                underlyings = self._snapshot_provider.get_underlyings()

                for underlying in underlyings:
                    try:
                        snapshot = await self._snapshot_provider.build_snapshot(underlying)
                        if snapshot is None or snapshot.atm_iv <= 0:
                            continue

                        await self.write_iv_snapshot(
                            symbol=underlying,
                            atm_iv=snapshot.atm_iv,
                            iv_rank=snapshot.iv_rank,
                            iv_percentile=snapshot.iv_percentile,
                            pcr_oi=snapshot.pcr_oi,
                            pcr_volume=snapshot.pcr_volume,
                            underlying_price=snapshot.underlying_price,
                        )
                    except Exception as exc:
                        logger.error(
                            "iv_snapshot_write_error",
                            underlying=underlying,
                            error=str(exc),
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("iv_write_loop_error", error=str(exc))
                await asyncio.sleep(5)

        logger.info("iv_write_loop_stopped")

    async def write_iv_snapshot(
        self,
        symbol: str,
        atm_iv: float,
        iv_rank: float = 0.0,
        iv_percentile: float = 0.0,
        pcr_oi: float = 0.0,
        pcr_volume: float = 0.0,
        underlying_price: float = 0.0,
    ) -> None:
        """Insert a single ATM IV snapshot row."""
        if self._pool is None:
            return

        query = """
        INSERT INTO atm_iv_snapshots
            (symbol, timestamp, atm_iv, iv_rank, iv_percentile, pcr_oi, pcr_volume, underlying_price)
        VALUES
            ($1, NOW(), $2, $3, $4, $5, $6, $7)
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                query, symbol, atm_iv, iv_rank, iv_percentile, pcr_oi, pcr_volume, underlying_price
            )

        logger.debug(
            "iv_snapshot_written",
            symbol=symbol,
            atm_iv=round(atm_iv, 4),
            iv_rank=round(iv_rank, 2),
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def check_health(self) -> tuple[bool, float]:
        """Returns (is_healthy, latency_ms)."""
        if self._pool is None:
            return False, 0.0

        start = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            latency = (time.monotonic() - start) * 1000
            return True, round(latency, 2)
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            logger.error("db_health_check_failed", error=str(exc))
            return False, round(latency, 2)
