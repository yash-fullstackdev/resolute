"""
signal_engine — Service entry point.

Responsibilities:
  - Connect to NATS and subscribe to tick data
  - Maintain in-memory options chain state via ChainProcessor
  - Compute Greeks (vectorised Black-Scholes), IV, PCR for all strikes
  - Publish enriched OptionsChainSnapshot every 5 seconds
  - Persist ATM IV to TimescaleDB every 60 seconds
  - Expose Prometheus metrics on :9091
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
# Structlog configuration (match auth_service style)
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

logger = structlog.get_logger(service="signal_engine")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

TICKS_RECEIVED = Counter(
    "signal_engine_ticks_received_total",
    "Total tick messages received from NATS",
    ["segment"],
)
CHAIN_SNAPSHOTS_PUBLISHED = Counter(
    "signal_engine_chain_snapshots_published_total",
    "Total chain snapshots published to NATS",
    ["underlying"],
)
CHAIN_CALC_DURATION = Histogram(
    "signal_engine_chain_calc_duration_seconds",
    "Time to compute full chain snapshot",
    ["underlying"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1],
)
UNDERLYINGS_TRACKED = Gauge(
    "signal_engine_underlyings_tracked",
    "Number of underlyings currently tracked",
)
IV_SNAPSHOT_WRITES = Counter(
    "signal_engine_iv_snapshot_writes_total",
    "Total ATM IV snapshots written to DB",
)
NATS_CONNECTED = Gauge(
    "signal_engine_nats_connected",
    "Whether NATS connection is active (1=yes, 0=no)",
)
DB_CONNECTED = Gauge(
    "signal_engine_db_connected",
    "Whether database connection is active (1=yes, 0=no)",
)

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9091"))


# ---------------------------------------------------------------------------
# Prometheus HTTP server (aiohttp)
# ---------------------------------------------------------------------------

async def _metrics_handler(request: web.Request) -> web.Response:
    """Return Prometheus metrics in exposition format."""
    body = generate_latest()
    return web.Response(body=body, content_type=CONTENT_TYPE_LATEST)


async def _health_handler(request: web.Request) -> web.Response:
    """Simple health endpoint."""
    return web.Response(
        text='{"status":"ok","service":"signal_engine"}',
        content_type="application/json",
    )


async def start_metrics_server() -> web.AppRunner:
    """Start lightweight HTTP server for Prometheus scraping."""
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
    """Start signal_engine: NATS + chain processor + DB + metrics."""
    logger.info("signal_engine_starting", version=APP_VERSION)

    # Import here to allow structlog to be configured first
    from .engine.chain_processor import ChainProcessor
    from .nats_client import SignalEngineNATSClient
    from .db import SignalEngineDB

    # ── Initialise components ────────────────────────────────────────
    db = SignalEngineDB()
    chain_processor = ChainProcessor(db=db)
    nats_client = SignalEngineNATSClient(chain_processor)

    # Shutdown event
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info("shutdown_signal_received", signal=sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    # ── Start metrics server ─────────────────────────────────────────
    metrics_runner = await start_metrics_server()

    # ── Connect to DB ────────────────────────────────────────────────
    try:
        await db.connect()
        DB_CONNECTED.set(1)
        logger.info("db_connected")
    except Exception as exc:
        logger.error("db_connection_failed", error=str(exc))
        DB_CONNECTED.set(0)
        # Service can run without DB (IV rank will return 0)

    # ── Connect to NATS ──────────────────────────────────────────────
    try:
        await nats_client.connect()
        NATS_CONNECTED.set(1)
    except Exception as exc:
        logger.error("nats_connection_failed", error=str(exc))
        NATS_CONNECTED.set(0)
        raise

    # ── Start IV snapshot writer ─────────────────────────────────────
    db.start_iv_writer(chain_processor)

    logger.info("signal_engine_started", version=APP_VERSION)

    # ── Wait for shutdown ────────────────────────────────────────────
    await shutdown_event.wait()
    logger.info("signal_engine_shutting_down")

    # ── Graceful shutdown ────────────────────────────────────────────
    await nats_client.close()
    NATS_CONNECTED.set(0)

    await db.close()
    DB_CONNECTED.set(0)

    await metrics_runner.cleanup()

    logger.info("signal_engine_stopped")


def run() -> None:
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
