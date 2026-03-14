"""
WorkerPoolManager -- manages lifecycle of all UserWorker instances.

One WorkerPoolManager runs as the main process.
Workers are asyncio tasks -- not separate processes.
At 500 concurrent users, each worker is lightweight (no I/O blocking).
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import structlog
from nats.aio.msg import Msg

from ..nats_client import WorkerPoolNATSClient
from ..db import AsyncDB
from ..config.user_config import UserConfigLoader, UserStrategyConfig
from ..capital_tier import get_capital_tier, is_strategy_allowed
from ..strategies import STRATEGY_REGISTRY
from ..discipline.plan_manager import PlanManager
from ..discipline.circuit_breaker import CircuitBreaker
from ..discipline.override_guard import OverrideGuard
from ..discipline.journal import TradeJournal
from ..risk.event_calendar import EventCalendar
from ..risk.position_sizer import PositionSizer
from ..risk.stop_loss import StopLossManager
from ..portfolio.manager import PortfolioManager
from .worker import UserWorker, DisciplineContext

logger = structlog.get_logger(service="user_worker_pool", module="manager")

AUTH_SERVICE_URL = os.environ.get("AUTH_SERVICE_URL", "http://localhost:8000")


class WorkerPoolManager:
    """Manages lifecycle of all UserWorker instances."""

    def __init__(
        self,
        nats: WorkerPoolNATSClient,
        db: AsyncDB,
    ) -> None:
        self.workers: dict[str, UserWorker] = {}  # keyed by tenant_id
        self._tasks: dict[str, asyncio.Task] = {}  # keyed by tenant_id
        self._nats = nats
        self._db = db
        self._config_loader = UserConfigLoader(db)
        self._event_calendar = EventCalendar()
        self._stop_loss_manager = StopLossManager()
        self._position_sizer = PositionSizer()
        self._shutting_down = False

    async def start(self) -> None:
        """Start the worker pool manager.

        1. Load event calendar
        2. Fetch list of active tenants from auth_service
        3. Spawn a UserWorker for each active tenant
        4. Subscribe to worker.started.* and worker.stopped.* NATS subjects
        """
        logger.info("worker_pool_starting")

        # Load event calendar
        await self._event_calendar.load_events(self._db)

        # Fetch active tenants
        active_tenants = await self._fetch_active_tenants()
        logger.info("active_tenants_fetched", count=len(active_tenants))

        # Spawn workers for all active tenants
        for tenant_id in active_tenants:
            try:
                await self.spawn_worker(tenant_id)
            except Exception as exc:
                logger.error(
                    "worker_spawn_failed",
                    tenant_id=tenant_id,
                    error=str(exc),
                )

        # Subscribe to dynamic worker lifecycle events
        await self._nats.subscribe("worker.started.*", cb=self._on_worker_started)
        await self._nats.subscribe("worker.stopped.*", cb=self._on_worker_stopped)

        logger.info(
            "worker_pool_started",
            active_workers=len(self.workers),
        )

    async def spawn_worker(self, tenant_id: str) -> UserWorker:
        """Spawn a new UserWorker for a tenant.

        1. Load tenant's strategy config from DB
        2. Load tenant's active trading plan
        3. Create UserWorker and start its event loop
        4. Register in self.workers
        """
        if tenant_id in self.workers:
            logger.warning("worker_already_exists", tenant_id=tenant_id)
            return self.workers[tenant_id]

        # Load user config
        config = await self._config_loader.load_config(tenant_id)

        # Determine capital tier
        capital_tier = get_capital_tier(config.portfolio_value_inr)

        # Instantiate only enabled and tier-allowed strategies
        strategies = []
        for name in config.enabled_strategy_names:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                logger.warning("unknown_strategy", tenant_id=tenant_id, strategy=name)
                continue
            if not is_strategy_allowed(cls.category, capital_tier):
                logger.info(
                    "strategy_tier_blocked",
                    tenant_id=tenant_id,
                    strategy=name,
                    category=cls.category.value,
                    user_tier=capital_tier.value,
                )
                continue
            strategies.append(cls())

        # Build discipline context
        plan_manager = PlanManager(db=self._db, nats=self._nats)
        circuit_breaker = CircuitBreaker(nats=self._nats)
        override_guard = OverrideGuard(
            circuit_breaker=circuit_breaker,
            db=self._db,
            nats=self._nats,
        )
        journal = TradeJournal(db=self._db, nats=self._nats)

        discipline = DisciplineContext(
            tenant_id=tenant_id,
            plan_manager=plan_manager,
            circuit_breaker=circuit_breaker,
            override_guard=override_guard,
            journal=journal,
        )

        # Load active plan from DB
        await plan_manager.load_plan_from_db(tenant_id)

        # Build portfolio manager
        portfolio = PortfolioManager(tenant_id=tenant_id, db=self._db)
        await portfolio.load_from_db()
        portfolio.portfolio_value_inr = config.portfolio_value_inr

        # Create worker
        worker = UserWorker(
            tenant_id=tenant_id,
            config=config,
            strategies=strategies,
            discipline=discipline,
            nats=self._nats,
            db=self._db,
            portfolio=portfolio,
            event_calendar=self._event_calendar,
            stop_loss_manager=self._stop_loss_manager,
            position_sizer=self._position_sizer,
            capital_tier=capital_tier,
        )

        self.workers[tenant_id] = worker

        # Start the worker event loop as an asyncio task
        task = asyncio.create_task(
            self._run_worker(tenant_id, worker),
            name=f"worker_{tenant_id}",
        )
        self._tasks[tenant_id] = task

        logger.info(
            "worker_spawned",
            tenant_id=tenant_id,
            strategies=[s.name for s in strategies],
            capital_tier=capital_tier.value,
            portfolio_value=config.portfolio_value_inr,
        )

        # Publish worker started event
        await self._nats.publish(
            f"worker.started.{tenant_id}",
            {"tenant_id": tenant_id, "strategies": [s.name for s in strategies]},
        )

        return worker

    async def _run_worker(self, tenant_id: str, worker: UserWorker) -> None:
        """Run worker with error handling and restart logic."""
        try:
            await worker.run()
        except asyncio.CancelledError:
            logger.info("worker_cancelled", tenant_id=tenant_id)
        except Exception as exc:
            logger.error(
                "worker_crashed",
                tenant_id=tenant_id,
                error=str(exc),
            )
            # Publish error event
            await self._nats.publish(
                f"worker.error.{tenant_id}",
                {"tenant_id": tenant_id, "error": str(exc)},
            )

    async def teardown_worker(self, tenant_id: str) -> None:
        """Gracefully stop a worker."""
        worker = self.workers.pop(tenant_id, None)
        task = self._tasks.pop(tenant_id, None)

        if worker:
            await worker.stop()

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info("worker_torn_down", tenant_id=tenant_id)

        await self._nats.publish(
            f"worker.stopped.{tenant_id}",
            {"tenant_id": tenant_id},
        )

    async def shutdown(self) -> None:
        """Gracefully shut down all workers."""
        logger.info("worker_pool_shutting_down", worker_count=len(self.workers))
        self._shutting_down = True

        tenant_ids = list(self.workers.keys())
        for tenant_id in tenant_ids:
            await self.teardown_worker(tenant_id)

        logger.info("worker_pool_shutdown_complete")

    # ------------------------------------------------------------------
    # NATS event handlers
    # ------------------------------------------------------------------

    async def _on_worker_started(self, msg: Msg) -> None:
        """Handle dynamic worker start request from auth_service."""
        try:
            data = json.loads(msg.data.decode())
            tenant_id = data.get("tenant_id")
            if not tenant_id:
                return

            # Only spawn if not already running
            if tenant_id not in self.workers:
                await self.spawn_worker(tenant_id)
                logger.info("dynamic_worker_spawned", tenant_id=tenant_id)
        except Exception as exc:
            logger.error("worker_started_handler_error", error=str(exc))

    async def _on_worker_stopped(self, msg: Msg) -> None:
        """Handle dynamic worker stop request."""
        try:
            data = json.loads(msg.data.decode())
            tenant_id = data.get("tenant_id")
            if not tenant_id:
                return

            if tenant_id in self.workers:
                await self.teardown_worker(tenant_id)
                logger.info("dynamic_worker_removed", tenant_id=tenant_id)
        except Exception as exc:
            logger.error("worker_stopped_handler_error", error=str(exc))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_active_tenants(self) -> list[str]:
        """Fetch list of active tenant IDs from auth_service."""
        url = f"{AUTH_SERVICE_URL}/internal/active-tenants"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("tenant_ids", [])
                logger.warning(
                    "active_tenants_fetch_failed",
                    status=resp.status_code,
                )
        except Exception as exc:
            logger.error("active_tenants_fetch_error", error=str(exc))

        # Fallback: try loading from DB
        try:
            rows = await self._db.fetch(
                "SELECT tenant_id FROM user_profiles WHERE is_active = true"
            )
            return [row["tenant_id"] for row in rows]
        except Exception as exc:
            logger.error("active_tenants_db_fallback_failed", error=str(exc))
            return []
