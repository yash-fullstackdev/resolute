"""
OverrideGuard -- manages override requests with mandatory friction.

Override friction design:
  - User submits override request -> 60-second mandatory cooldown
  - During cooldown, user sees last 5 override outcomes (P&L impact)
  - After 60 seconds, user must re-confirm to proceed
  - Override is logged permanently regardless of outcome
  - If circuit breaker is HALTED, override requests are automatically rejected

The platform does NOT prevent overrides -- it creates deliberate friction
and surfaces data that makes the cost of overriding visible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="override_guard")


@dataclass
class OverrideRequest:
    """A pending or completed override request."""
    id: str
    user_id: str
    position_id: str
    override_type: str        # "STOP_LOSS_MOVE" | "EARLY_EXIT" | "TIME_STOP_EXTEND"
    original_value: float
    proposed_value: float
    reason: str
    requested_at: datetime
    cooldown_expires_at: datetime
    status: str = "PENDING"   # "PENDING" | "CONFIRMED" | "EXPIRED" | "REJECTED"
    confirmed_at: datetime | None = None
    outcome_pnl_inr: float | None = None  # filled in after position closes


@dataclass
class OverrideHistorySummary:
    """Summary of past override outcomes shown during cooldown."""
    total_overrides: int
    overrides_that_helped_inr: float   # P&L saved by overriding
    overrides_that_hurt_inr: float     # Additional loss from overriding
    net_override_impact_inr: float     # net = helped - hurt (usually negative)
    last_overrides: list[dict]         # [{date, type, outcome_inr}, ...]


class OverrideGuard:
    """Manages override requests with mandatory cooldown friction."""

    COOLDOWN_SECONDS = 60

    def __init__(self, circuit_breaker=None, db=None, nats=None) -> None:
        self._circuit_breaker = circuit_breaker
        self._db = db
        self._nats = nats
        self._pending_requests: dict[str, OverrideRequest] = {}  # request_id -> request
        self._history: dict[str, list[OverrideRequest]] = {}     # user_id -> list of all requests

    def request_override(
        self,
        user_id: str,
        position_id: str,
        override_type: str,
        proposed_value: float,
        reason: str,
        original_value: float = 0.0,
    ) -> OverrideRequest | tuple[None, str]:
        """Create an override request with mandatory cooldown.

        Returns OverrideRequest if accepted, or (None, rejection_reason) if rejected.
        """
        # Circuit breaker check -- if halted, reject immediately
        if self._circuit_breaker and self._circuit_breaker.is_user_halted(user_id):
            logger.warning(
                "override_rejected_circuit_breaker",
                tenant_id=user_id,
                position_id=position_id,
            )
            return None, "Circuit breaker is HALTED. Override requests are not accepted."

        # Validate reason (min 10 chars)
        if not reason or len(reason.strip()) < 10:
            return None, "Override reason must be at least 10 characters."

        # Validate override type
        valid_types = {"STOP_LOSS_MOVE", "EARLY_EXIT", "TIME_STOP_EXTEND"}
        if override_type not in valid_types:
            return None, f"Invalid override type. Must be one of: {valid_types}"

        now = datetime.now(timezone.utc)
        cooldown_expires = now + timedelta(seconds=self.COOLDOWN_SECONDS)

        request = OverrideRequest(
            id=str(uuid.uuid4()),
            user_id=user_id,
            position_id=position_id,
            override_type=override_type,
            original_value=original_value,
            proposed_value=proposed_value,
            reason=reason.strip(),
            requested_at=now,
            cooldown_expires_at=cooldown_expires,
            status="PENDING",
        )

        self._pending_requests[request.id] = request

        # Track in user history
        if user_id not in self._history:
            self._history[user_id] = []
        self._history[user_id].append(request)

        logger.info(
            "override_requested",
            tenant_id=user_id,
            request_id=request.id,
            override_type=override_type,
            position_id=position_id,
            cooldown_expires=cooldown_expires.isoformat(),
        )

        return request

    def confirm_override(
        self,
        override_request_id: str,
        user_id: str,
    ) -> tuple[bool, str]:
        """Confirm an override after cooldown period.

        Validates:
        1. Cooldown has elapsed
        2. Circuit breaker is not halted
        3. Request is still PENDING (not expired)

        Returns (approved, message).
        """
        request = self._pending_requests.get(override_request_id)
        if request is None:
            return False, "Override request not found."

        if request.user_id != user_id:
            return False, "Override request belongs to a different user."

        if request.status != "PENDING":
            return False, f"Override request is already {request.status}."

        # Check circuit breaker again
        if self._circuit_breaker and self._circuit_breaker.is_user_halted(user_id):
            request.status = "REJECTED"
            return False, "Circuit breaker is HALTED. Override rejected."

        # Check cooldown
        now = datetime.now(timezone.utc)
        if now < request.cooldown_expires_at:
            remaining = (request.cooldown_expires_at - now).seconds
            return False, (
                f"Cooldown still active. {remaining} seconds remaining. "
                f"Please wait before confirming."
            )

        # Expire check: request is valid for 5 minutes after cooldown
        max_confirm_time = request.cooldown_expires_at + timedelta(minutes=5)
        if now > max_confirm_time:
            request.status = "EXPIRED"
            return False, "Override request has expired. Please submit a new request."

        # Approve
        request.status = "CONFIRMED"
        request.confirmed_at = now

        logger.warning(
            "override_confirmed",
            tenant_id=user_id,
            request_id=override_request_id,
            override_type=request.override_type,
            position_id=request.position_id,
            original_value=request.original_value,
            proposed_value=request.proposed_value,
            reason=request.reason,
        )

        return True, (
            f"Override confirmed for {request.override_type}. "
            f"This has been logged permanently."
        )

    def get_override_history_summary(
        self,
        user_id: str,
        last_n: int = 5,
    ) -> OverrideHistorySummary:
        """Return summary of past override outcomes shown during cooldown.

        Includes last N override attempts and their P&L impact.
        """
        all_requests = self._history.get(user_id, [])

        # Filter to confirmed overrides with outcomes
        confirmed = [
            r for r in all_requests
            if r.status == "CONFIRMED" and r.outcome_pnl_inr is not None
        ]

        helped_inr = sum(
            r.outcome_pnl_inr for r in confirmed
            if r.outcome_pnl_inr is not None and r.outcome_pnl_inr > 0
        )
        hurt_inr = sum(
            abs(r.outcome_pnl_inr) for r in confirmed
            if r.outcome_pnl_inr is not None and r.outcome_pnl_inr < 0
        )
        net_impact = helped_inr - hurt_inr

        # Build last N entries
        recent = sorted(all_requests, key=lambda r: r.requested_at, reverse=True)[:last_n]
        last_overrides = [
            {
                "date": r.requested_at.isoformat(),
                "type": r.override_type,
                "status": r.status,
                "outcome_inr": r.outcome_pnl_inr,
            }
            for r in recent
        ]

        return OverrideHistorySummary(
            total_overrides=len(all_requests),
            overrides_that_helped_inr=helped_inr,
            overrides_that_hurt_inr=hurt_inr,
            net_override_impact_inr=net_impact,
            last_overrides=last_overrides,
        )

    def get_pending_requests_for_position(self, position_id: str) -> list[OverrideRequest]:
        """Return all override requests (any status) for a position."""
        return [
            r for r in self._pending_requests.values()
            if r.position_id == position_id
        ]

    def get_override_count_for_position(self, position_id: str) -> int:
        """Count all override requests for a position."""
        count = 0
        for requests in self._history.values():
            for r in requests:
                if r.position_id == position_id:
                    count += 1
        return count

    def has_confirmed_override_for_position(self, position_id: str) -> bool:
        """Check if any confirmed override exists for a position."""
        for requests in self._history.values():
            for r in requests:
                if r.position_id == position_id and r.status == "CONFIRMED":
                    return True
        return False

    async def persist_override(self, request: OverrideRequest) -> None:
        """Persist override request to the database."""
        if self._db is None:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO override_audit_log
                    (id, user_id, position_id, override_type, original_value,
                     proposed_value, reason, requested_at, cooldown_expires_at,
                     status, confirmed_at, outcome_pnl_inr)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    confirmed_at = EXCLUDED.confirmed_at,
                    outcome_pnl_inr = EXCLUDED.outcome_pnl_inr
                """,
                request.id,
                request.user_id,
                request.position_id,
                request.override_type,
                request.original_value,
                request.proposed_value,
                request.reason,
                request.requested_at,
                request.cooldown_expires_at,
                request.status,
                request.confirmed_at,
                request.outcome_pnl_inr,
                tenant_id=request.user_id,
            )
        except Exception as exc:
            logger.error(
                "override_persist_failed",
                request_id=request.id,
                error=str(exc),
            )
