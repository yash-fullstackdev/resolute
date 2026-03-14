"""
Database setup: async SQLAlchemy engine/session + Row-Level Security context manager.

Same pattern as auth_service — uses SET LOCAL app.current_tenant_id before
every tenant-scoped query for RLS double enforcement.
"""

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = structlog.get_logger(service="dashboard_api", module="db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/resolute",
)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session (no RLS)."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def rls_session(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager that creates an async session with Row-Level Security
    enforced via SET LOCAL.

    SET LOCAL scopes the setting to the current transaction, so it is
    automatically cleared when the transaction ends — no risk of leaking
    tenant context across requests.

    Usage:
        async with rls_session(tenant_id) as session:
            result = await session.execute(select(...))
    """
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SET LOCAL app.current_tenant_id = :tid"),
                {"tid": tenant_id},
            )
            logger.debug("rls_context_set", tenant_id=tenant_id)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise


async def check_db_health() -> tuple[bool, float]:
    """
    Check database connectivity and measure latency.

    Returns (is_healthy, latency_ms).
    """
    start = time.monotonic()
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        latency_ms = (time.monotonic() - start) * 1000
        return True, round(latency_ms, 2)
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        logger.error("db_health_check_failed", error=str(exc))
        return False, round(latency_ms, 2)


async def dispose_engine() -> None:
    """Dispose of the SQLAlchemy engine (call on shutdown)."""
    await engine.dispose()
    logger.info("db_engine_disposed")
