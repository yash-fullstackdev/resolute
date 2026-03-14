"""
Async NATS client for user_worker_pool.

Manages the shared NATS connection used by all UserWorker instances.
Each worker subscribes to chain.* and per-tenant subjects via this client.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable, Coroutine

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
import structlog

logger = structlog.get_logger(service="user_worker_pool", module="nats_client")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
HEARTBEAT_INTERVAL = 10.0


class WorkerPoolNATSClient:
    """Shared NATS connection for the entire worker pool process."""

    def __init__(self) -> None:
        self._nc: NATSClient | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._shutting_down = False
        self._subs: list = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to NATS with infinite reconnect."""
        self._nc = await nats.connect(
            NATS_URL,
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
            closed_cb=self._on_close,
        )
        logger.info("nats_connected", url=NATS_URL)
        self._heartbeat_task = asyncio.create_task(self._periodic_heartbeat())

    async def close(self) -> None:
        """Gracefully drain and close."""
        self._shutting_down = True

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:
                pass

        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            logger.info("nats_drained")

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and not self._nc.is_closed

    # ------------------------------------------------------------------
    # Subscribe / Publish helpers
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        subject: str,
        cb: Callable[[Msg], Coroutine[Any, Any, None]],
    ):
        """Subscribe and track the subscription for cleanup."""
        if self._nc is None:
            raise RuntimeError("NATS not connected")
        sub = await self._nc.subscribe(subject, cb=cb)
        self._subs.append(sub)
        logger.debug("nats_subscribed", subject=subject)
        return sub

    async def publish(self, subject: str, data: dict | bytes) -> None:
        """Publish a message (dict is auto-serialised to JSON bytes)."""
        if self._nc is None or self._nc.is_closed:
            logger.warning("nats_publish_skipped_not_connected", subject=subject)
            return
        if isinstance(data, dict):
            payload = json.dumps(data).encode()
        else:
            payload = data
        await self._nc.publish(subject, payload)

    async def unsubscribe(self, sub) -> None:
        """Unsubscribe a single subscription."""
        try:
            await sub.unsubscribe()
            if sub in self._subs:
                self._subs.remove(sub)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _periodic_heartbeat(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._nc and not self._nc.is_closed:
                    payload = json.dumps({
                        "service": "user_worker_pool",
                        "timestamp": time.time(),
                    }).encode()
                    await self._nc.publish("heartbeat.user_worker_pool", payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("heartbeat_error", error=str(exc))

    # ------------------------------------------------------------------
    # NATS callbacks
    # ------------------------------------------------------------------

    async def _on_error(self, exc: Exception) -> None:
        logger.error("nats_error", error=str(exc))

    async def _on_disconnect(self) -> None:
        logger.warning("nats_disconnected")

    async def _on_reconnect(self) -> None:
        logger.info("nats_reconnected")

    async def _on_close(self) -> None:
        logger.info("nats_closed")
