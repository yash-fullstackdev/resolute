"""
NATS async client for signal_engine.

Subscribes to:
  - ticks.nse.*   (index + F&O ticks)
  - ticks.mcx.*   (commodity ticks)

Publishes:
  - chain.{segment}.{underlying}       every 5 seconds
  - chain.request.{underlying}         request-reply (on demand)

Also publishes heartbeat.signal_engine every 10 seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
import structlog

from .engine.chain_processor import ChainProcessor

logger = structlog.get_logger(service="signal_engine", module="nats_client")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
CHAIN_PUBLISH_INTERVAL = float(os.environ.get("CHAIN_PUBLISH_INTERVAL", "5.0"))
HEARTBEAT_INTERVAL = 10.0


class SignalEngineNATSClient:
    """Manages NATS connection, subscriptions, and periodic chain publishing."""

    def __init__(self, chain_processor: ChainProcessor):
        self._nc: NATSClient | None = None
        self._chain_processor = chain_processor
        self._publish_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._shutting_down = False
        self._subs: list = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to NATS and subscribe to tick subjects."""
        self._nc = await nats.connect(
            NATS_URL,
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,  # infinite reconnect
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
            closed_cb=self._on_close,
        )
        logger.info("nats_connected", url=NATS_URL)

        # Subscribe to tick subjects
        sub_nse = await self._nc.subscribe("ticks.nse.>", cb=self._on_tick)
        sub_mcx = await self._nc.subscribe("ticks.mcx.>", cb=self._on_tick)
        self._subs.extend([sub_nse, sub_mcx])
        logger.info("nats_subscribed", subjects=["ticks.nse.>", "ticks.mcx.>"])

        # Subscribe to request-reply for chain requests
        sub_chain_req = await self._nc.subscribe(
            "chain.request.*", cb=self._on_chain_request
        )
        self._subs.append(sub_chain_req)
        logger.info("nats_subscribed_request_reply", subject="chain.request.*")

        # Start periodic publishers
        self._publish_task = asyncio.create_task(self._periodic_chain_publish())
        self._heartbeat_task = asyncio.create_task(self._periodic_heartbeat())

    async def close(self) -> None:
        """Gracefully shut down NATS subscriptions and connection."""
        self._shutting_down = True

        # Cancel background tasks
        for task in (self._publish_task, self._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Drain subscriptions
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:
                pass

        if self._nc is not None and not self._nc.is_closed:
            await self._nc.drain()
            logger.info("nats_drained")

    # ------------------------------------------------------------------
    # Tick handler
    # ------------------------------------------------------------------

    async def _on_tick(self, msg: Msg) -> None:
        """Handle incoming tick messages."""
        try:
            tick = json.loads(msg.data.decode())
            self._chain_processor.process_tick(tick)
        except json.JSONDecodeError as exc:
            logger.warning("tick_decode_error", subject=msg.subject, error=str(exc))
        except Exception as exc:
            logger.error("tick_processing_error", subject=msg.subject, error=str(exc))

    # ------------------------------------------------------------------
    # Request-reply: chain.request.{underlying}
    # ------------------------------------------------------------------

    async def _on_chain_request(self, msg: Msg) -> None:
        """Handle chain request-reply.

        Subject format: chain.request.{underlying}
        Responds with the full OptionsChainSnapshot JSON.
        """
        try:
            # Extract underlying from subject
            parts = msg.subject.split(".")
            if len(parts) < 3:
                return
            underlying = parts[2]

            snapshot = await self._chain_processor.build_snapshot(underlying)
            if snapshot is None:
                payload = json.dumps({"error": "no_chain_data", "underlying": underlying})
            else:
                payload = json.dumps(snapshot.to_dict())

            if msg.reply:
                await self._nc.publish(msg.reply, payload.encode())
                logger.debug("chain_request_replied", underlying=underlying)
        except Exception as exc:
            logger.error("chain_request_error", error=str(exc))

    # ------------------------------------------------------------------
    # Periodic chain publish (every 5s)
    # ------------------------------------------------------------------

    async def _periodic_chain_publish(self) -> None:
        """Publish chain snapshots for all tracked underlyings every N seconds."""
        logger.info("chain_publish_loop_started", interval_s=CHAIN_PUBLISH_INTERVAL)

        while not self._shutting_down:
            try:
                await asyncio.sleep(CHAIN_PUBLISH_INTERVAL)

                underlyings = self._chain_processor.get_underlyings()
                for underlying in underlyings:
                    try:
                        snapshot = await self._chain_processor.build_snapshot(underlying)
                        if snapshot is None:
                            continue

                        segment = self._chain_processor.get_segment(underlying)
                        # Normalise segment for subject: NSE_FO -> nse, MCX -> mcx
                        segment_key = "mcx" if segment == "MCX" else "nse"
                        subject = f"chain.{segment_key}.{underlying}"

                        payload = json.dumps(snapshot.to_dict()).encode()
                        await self._nc.publish(subject, payload)

                        logger.debug(
                            "chain_snapshot_published",
                            subject=subject,
                            underlying=underlying,
                            num_strikes=len(snapshot.strikes),
                        )
                    except Exception as exc:
                        logger.error(
                            "chain_publish_error",
                            underlying=underlying,
                            error=str(exc),
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("chain_publish_loop_error", error=str(exc))
                await asyncio.sleep(1)

        logger.info("chain_publish_loop_stopped")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _periodic_heartbeat(self) -> None:
        """Publish heartbeat every 10 seconds."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._nc and not self._nc.is_closed:
                    payload = json.dumps({
                        "service": "signal_engine",
                        "timestamp": time.time(),
                        "underlyings_tracked": len(self._chain_processor.get_underlyings()),
                    }).encode()
                    await self._nc.publish("heartbeat.signal_engine", payload)
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
