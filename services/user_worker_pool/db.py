"""
Async database client for user_worker_pool with Row-Level Security (RLS).

Provides an async context manager that sets ``app.current_tenant`` on every
connection so that PostgreSQL RLS policies automatically scope queries to the
active tenant.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg
import structlog

logger = structlog.get_logger(service="user_worker_pool", module="db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/resolute",
)
_DB_DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class AsyncDB:
    """Async PostgreSQL client with per-tenant RLS context."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            _DB_DSN,
            min_size=5,
            max_size=25,
            command_timeout=10,
        )
        logger.info("db_connected", dsn=_DB_DSN.split("@")[-1])

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("db_pool_closed")

    # ------------------------------------------------------------------
    # RLS context manager
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def tenant_connection(self, tenant_id: str) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection and set the RLS tenant context.

        Usage::

            async with db.tenant_connection(tenant_id) as conn:
                rows = await conn.fetch("SELECT * FROM positions")
        """
        if self._pool is None:
            raise RuntimeError("Database pool not initialised")

        # Validate tenant_id is a UUID to prevent injection
        import uuid as _uuid
        try:
            _uuid.UUID(str(tenant_id))
        except ValueError:
            raise ValueError(f"Invalid tenant_id format: {tenant_id}")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # SET LOCAL requires a transaction; match RLS policy setting name
                await conn.execute(
                    f"SET LOCAL app.current_tenant_id = '{tenant_id}'"
                )
                yield conn

    # ------------------------------------------------------------------
    # Raw pool access (for non-tenant-scoped queries)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a plain connection without RLS context."""
        if self._pool is None:
            raise RuntimeError("Database pool not initialised")
        async with self._pool.acquire() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Convenience query helpers
    # ------------------------------------------------------------------

    async def fetch(
        self, query: str, *args, tenant_id: str | None = None
    ) -> list[asyncpg.Record]:
        if tenant_id:
            async with self.tenant_connection(tenant_id) as conn:
                return await conn.fetch(query, *args)
        async with self.connection() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(
        self, query: str, *args, tenant_id: str | None = None
    ) -> Optional[asyncpg.Record]:
        if tenant_id:
            async with self.tenant_connection(tenant_id) as conn:
                return await conn.fetchrow(query, *args)
        async with self.connection() as conn:
            return await conn.fetchrow(query, *args)

    async def execute(
        self, query: str, *args, tenant_id: str | None = None
    ) -> str:
        if tenant_id:
            async with self.tenant_connection(tenant_id) as conn:
                return await conn.execute(query, *args)
        async with self.connection() as conn:
            return await conn.execute(query, *args)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def check_health(self) -> tuple[bool, float]:
        if self._pool is None:
            return False, 0.0
        start = time.monotonic()
        try:
            async with self.connection() as conn:
                await conn.fetchval("SELECT 1")
            latency = (time.monotonic() - start) * 1000
            return True, round(latency, 2)
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            logger.error("db_health_check_failed", error=str(exc))
            return False, round(latency, 2)
