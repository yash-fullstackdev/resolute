"""
Internal service-to-service endpoints.

These endpoints are not user-facing. They require AUTH_INTERNAL_TOKEN header
for authentication (simulating mTLS-based service identity).

GET  /internal/tenant/{tenant_id}       — Full tenant record
GET  /internal/broker-creds/{tenant_id} — Decrypted broker credentials
GET  /internal/active-tenants           — List of tenants with active trading sessions
"""

import json
import os
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import decrypt
from ..db import get_session
from ..models.schemas import (
    ActiveTenantItem,
    ActiveTenantsResponse,
    BrokerCredentialRecord,
    ErrorDetail,
    ErrorResponse,
    TenantRecord,
)

logger = structlog.get_logger(service="auth_service")
router = APIRouter(prefix="/internal", tags=["internal"])

AUTH_INTERNAL_TOKEN = os.environ.get("AUTH_INTERNAL_TOKEN", "")


def _error_response(status_code: int, code: str, message: str, details: dict | None = None):
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {}),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _validate_internal_token(request: Request) -> bool:
    """Validate the internal service-to-service bearer token."""
    if not AUTH_INTERNAL_TOKEN:
        logger.error("internal_token_not_configured")
        return False
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    return token == AUTH_INTERNAL_TOKEN


async def _audit_log(
    session: AsyncSession,
    event_type: str,
    tenant_id: str | None,
    details: dict,
    ip_address: str | None = None,
):
    try:
        await session.execute(
            text(
                """
                INSERT INTO audit_events (id, event_type, tenant_id, details, ip_address, created_at)
                VALUES (:id, :event_type, :tenant_id, :details, :ip_address, NOW())
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "event_type": event_type,
                "tenant_id": tenant_id,
                "details": json.dumps(details),
                "ip_address": ip_address,
            },
        )
    except Exception as exc:
        logger.error("audit_log_db_write_failed", error=str(exc), event_type=event_type)

    logger.info("audit_event", event_type=event_type, tenant_id=tenant_id, **details)


# ── GET /internal/tenant/{tenant_id} ─────────────────────────────────────────


@router.get("/tenant/{tenant_id}", response_model=TenantRecord)
async def get_tenant(
    tenant_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _validate_internal_token(request):
        return _error_response(401, "UNAUTHORIZED", "Invalid or missing internal service token.")

    result = await session.execute(
        text(
            """
            SELECT id, email, name, subscription_tier, subscription_status,
                   trial_ends_at, subscription_ends_at, created_at, is_active, email_verified
            FROM tenants WHERE id = :tid
            """
        ),
        {"tid": tenant_id},
    )
    row = result.fetchone()

    if not row:
        return _error_response(404, "NOT_FOUND", f"Tenant {tenant_id} not found.")

    return TenantRecord(
        id=row.id,
        email=row.email,
        name=row.name,
        subscription_tier=row.subscription_tier,
        subscription_status=row.subscription_status,
        trial_ends_at=row.trial_ends_at,
        subscription_ends_at=row.subscription_ends_at,
        created_at=row.created_at,
        is_active=row.is_active,
        email_verified=row.email_verified,
    )


# ── GET /internal/broker-creds/{tenant_id} ───────────────────────────────────


@router.get("/broker-creds/{tenant_id}", response_model=list[BrokerCredentialRecord])
async def get_broker_credentials(
    tenant_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _validate_internal_token(request):
        return _error_response(401, "UNAUTHORIZED", "Invalid or missing internal service token.")

    ip = request.client.host if request.client else "unknown"

    result = await session.execute(
        text(
            """
            SELECT broker, api_key_encrypted, api_secret_encrypted, client_id_encrypted,
                   totp_secret_encrypted, access_token_encrypted, token_expires_at, is_verified
            FROM user_broker_credentials
            WHERE tenant_id = :tid
            """
        ),
        {"tid": tenant_id},
    )
    rows = result.fetchall()

    if not rows:
        return _error_response(404, "NOT_FOUND", f"No broker credentials found for tenant {tenant_id}.")

    credentials = []
    for row in rows:
        # Decrypt all fields
        api_key = decrypt(row.api_key_encrypted)
        api_secret = decrypt(row.api_secret_encrypted)
        client_id = decrypt(row.client_id_encrypted)
        totp_secret = decrypt(row.totp_secret_encrypted)

        access_token = None
        if row.access_token_encrypted:
            access_token = decrypt(row.access_token_encrypted)

        credentials.append(
            BrokerCredentialRecord(
                broker=row.broker,
                api_key=api_key,
                api_secret=api_secret,
                client_id=client_id,
                totp_secret=totp_secret,
                access_token=access_token,
                token_expires_at=row.token_expires_at,
                is_verified=row.is_verified,
            )
        )

    # Audit log every credential decryption
    await _audit_log(
        session, "BROKER_CREDENTIAL_DECRYPTED", tenant_id,
        {"broker_count": len(credentials), "purpose": "internal_service_request"},
        ip_address=ip,
    )
    await session.commit()

    logger.info(
        "broker_creds_served_internal",
        tenant_id=tenant_id,
        broker_count=len(credentials),
    )
    return credentials


# ── GET /internal/active-tenants ──────────────────────────────────────────────


@router.get("/active-tenants", response_model=ActiveTenantsResponse)
async def get_active_tenants(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _validate_internal_token(request):
        return _error_response(401, "UNAUTHORIZED", "Invalid or missing internal service token.")

    # Active tenants: is_active=true, have at least one verified broker credential,
    # subscription is ACTIVE or TRIAL
    result = await session.execute(
        text(
            """
            SELECT t.id, t.email, t.subscription_tier,
                   ARRAY_AGG(DISTINCT ubc.broker) FILTER (WHERE ubc.broker IS NOT NULL) AS brokers
            FROM tenants t
            LEFT JOIN user_broker_credentials ubc
                ON ubc.tenant_id = t.id AND ubc.is_verified = true
            WHERE t.is_active = true
              AND t.subscription_status IN ('ACTIVE', 'TRIAL')
            GROUP BY t.id, t.email, t.subscription_tier
            HAVING COUNT(ubc.id) > 0
            ORDER BY t.email
            """
        ),
    )
    rows = result.fetchall()

    tenants = [
        ActiveTenantItem(
            tenant_id=row.id,
            email=row.email,
            tier=row.subscription_tier,
            brokers=row.brokers or [],
        )
        for row in rows
    ]

    logger.info("active_tenants_served", count=len(tenants))
    return ActiveTenantsResponse(tenants=tenants, count=len(tenants))
