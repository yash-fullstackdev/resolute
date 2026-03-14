"""
user_worker_pool -- Service entry point.

Responsibilities:
  - Connect to NATS and PostgreSQL
  - Spawn per-user UserWorker instances via WorkerPoolManager
  - Expose Prometheus metrics on :9093
  - Handle graceful shutdown (SIGINT, SIGTERM)
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import structlog
from aiohttp import web
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Structlog configuration
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        int(os.environ.get("LOG_LEVEL", "20"))
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(service="user_worker_pool")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

WORKERS_ACTIVE = Gauge(
    "user_worker_pool_workers_active",
    "Number of active UserWorker instances",
)
SIGNALS_GENERATED = Counter(
    "user_worker_pool_signals_generated_total",
    "Total signals generated across all workers",
    ["strategy"],
)
ORDERS_PUBLISHED = Counter(
    "user_worker_pool_orders_published_total",
    "Total validated orders published to NATS",
)
CIRCUIT_BREAKER_HALTS = Counter(
    "user_worker_pool_circuit_breaker_halts_total",
    "Total circuit breaker halts",
)
CHAIN_EVAL_DURATION = Histogram(
    "user_worker_pool_chain_eval_duration_seconds",
    "Time to evaluate chain snapshot for one user",
    buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05],
)
NATS_CONNECTED = Gauge(
    "user_worker_pool_nats_connected",
    "NATS connection status (1=connected, 0=disconnected)",
)
DB_CONNECTED = Gauge(
    "user_worker_pool_db_connected",
    "Database connection status (1=connected, 0=disconnected)",
)

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9093"))


# ---------------------------------------------------------------------------
# Prometheus HTTP server
# ---------------------------------------------------------------------------

async def _metrics_handler(request: web.Request) -> web.Response:
    body = generate_latest()
    return web.Response(body=body, content_type=CONTENT_TYPE_LATEST)


async def _health_handler(request: web.Request) -> web.Response:
    return web.Response(
        text='{"status":"ok","service":"user_worker_pool"}',
        content_type="application/json",
    )


async def start_metrics_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/metrics", _metrics_handler)
    app.router.add_get("/health", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", METRICS_PORT)
    await site.start()
    logger.info("metrics_server_started", port=METRICS_PORT)
    return runner


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Start user_worker_pool: NATS + DB + WorkerPoolManager + metrics."""
    logger.info("user_worker_pool_starting", version=APP_VERSION)

    from .nats_client import WorkerPoolNATSClient
    from .db import AsyncDB
    from .pool.manager import WorkerPoolManager

    # Initialise components
    nats_client = WorkerPoolNATSClient()
    db = AsyncDB()

    # Shutdown event
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info("shutdown_signal_received", signal=sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    # Start metrics server
    metrics_runner = await start_metrics_server()

    # Connect to database
    try:
        await db.connect()
        DB_CONNECTED.set(1)
        logger.info("db_connected")
    except Exception as exc:
        logger.error("db_connection_failed", error=str(exc))
        DB_CONNECTED.set(0)

    # Connect to NATS
    try:
        await nats_client.connect()
        NATS_CONNECTED.set(1)
    except Exception as exc:
        logger.error("nats_connection_failed", error=str(exc))
        NATS_CONNECTED.set(0)
        raise

    # Start worker pool manager
    pool_manager = WorkerPoolManager(nats=nats_client, db=db)
    try:
        await pool_manager.start()
        WORKERS_ACTIVE.set(len(pool_manager.workers))
    except Exception as exc:
        logger.error("worker_pool_start_failed", error=str(exc))

    logger.info(
        "user_worker_pool_started",
        version=APP_VERSION,
        active_workers=len(pool_manager.workers),
    )

    # Wait for shutdown signal
    await shutdown_event.wait()
    logger.info("user_worker_pool_shutting_down")

    # Graceful shutdown
    await pool_manager.shutdown()
    WORKERS_ACTIVE.set(0)

    await nats_client.close()
    NATS_CONNECTED.set(0)

    await db.close()
    DB_CONNECTED.set(0)

    await metrics_runner.cleanup()

    logger.info("user_worker_pool_stopped")


def run() -> None:
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
