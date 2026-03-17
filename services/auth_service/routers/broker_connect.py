"""
Broker credential vault router: connect, status, disconnect, refresh.

All sensitive fields are AES-256-GCM encrypted at rest.
Every credential store/decrypt event is audit-logged.
"""

import json
import uuid
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import decrypt, encrypt
from ..core.jwt import validate_access_token
from ..db import get_session
from ..models.schemas import (
    BrokerConnectRequest,
    BrokerConnectResponse,
    BrokerDisconnectResponse,
    BrokerRefreshResponse,
    BrokerStatusItem,
    BrokerStatusResponse,
    ErrorDetail,
    ErrorResponse,
)

logger = structlog.get_logger(service="auth_service")
router = APIRouter(prefix="/auth/v1/broker", tags=["broker"])


def _error_response(status_code: int, code: str, message: str, details: dict | None = None):
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {}),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def _get_tenant_from_token(request: Request) -> dict | None:
    """Extract and validate tenant claims from the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    redis = request.app.state.redis
    try:
        claims = await validate_access_token(redis, token)
        return claims
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _audit_log(
    session: AsyncSession,
    event_type: str,
    tenant_id: str | None,
    details: dict,
    ip_address: str | None = None,
    user_agent: str | None = None,
):
    try:
        await session.execute(
            text(
                """
                INSERT INTO audit_events (id, event_type, tenant_id, details, ip_address, user_agent, created_at)
                VALUES (:id, :event_type, :tenant_id, :details, :ip_address, :user_agent, NOW())
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "event_type": event_type,
                "tenant_id": tenant_id,
                "details": json.dumps(details),
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
    except Exception as exc:
        logger.error("audit_log_db_write_failed", error=str(exc), event_type=event_type)

    logger.info(
        "audit_event",
        event_type=event_type,
        tenant_id=tenant_id,
        ip_address=ip_address,
        **details,
    )


async def _verify_broker_credentials(broker: str, api_key: str, api_secret: str, client_id: str) -> bool:
    """
    Perform a test API call to verify that broker credentials are valid.
    Calls the broker's profile/ping endpoint.

    Returns True if credentials work, False otherwise.
    """
    try:
        if broker == "dhan":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.dhan.co/v2/clients/profile",
                    headers={
                        "Content-Type": "application/json",
                        "access-token": api_key,
                        "client-id": client_id,
                    },
                )
                return resp.status_code == 200

        elif broker == "zerodha":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.kite.trade/user/profile",
                    headers={
                        "X-Kite-Version": "3",
                        "Authorization": f"token {api_key}:{api_secret}",
                    },
                )
                return resp.status_code == 200

        return False
    except httpx.RequestError as exc:
        logger.warning("broker_verification_network_error", broker=broker, error=str(exc))
        return False
    except Exception as exc:
        logger.error("broker_verification_unexpected_error", broker=broker, error=str(exc))
        return False


# ── POST /broker/connect ─────────────────────────────────────────────────────


@router.post("/connect", response_model=BrokerConnectResponse, status_code=201)
async def connect_broker(
    request: Request,
    body: BrokerConnectRequest,
    session: AsyncSession = Depends(get_session),
):
    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")

    # Check tier allows broker connect
    from ..core.subscription import Tier, check_feature_access

    try:
        user_tier = Tier(claims["tier"])
    except ValueError:
        return _error_response(403, "FORBIDDEN", "Invalid subscription tier.")

    if not check_feature_access(user_tier, "broker_connect"):
        return _error_response(
            403, "CAPITAL_TIER_BLOCKED",
            "Broker connectivity requires Semi-Auto tier or higher.",
            {"current_tier": claims["tier"], "required_tier": "SEMI_AUTO"},
        )

    # Check if this broker is already connected
    existing = await session.execute(
        text(
            "SELECT id FROM user_broker_credentials WHERE tenant_id = :tid AND broker = :broker"
        ),
        {"tid": tenant_id, "broker": body.broker},
    )
    if existing.fetchone():
        return _error_response(
            409, "VALIDATION_ERROR",
            f"Broker '{body.broker}' is already connected. Disconnect first to reconnect.",
        )

    # Verify credentials with a test API call
    is_verified = await _verify_broker_credentials(
        body.broker, body.api_key, body.api_secret, body.client_id
    )

    # Encrypt all sensitive fields
    api_key_enc = encrypt(body.api_key)
    api_secret_enc = encrypt(body.api_secret)
    client_id_enc = encrypt(body.client_id)
    totp_secret_enc = encrypt(body.totp_secret)

    now = datetime.now(timezone.utc)
    cred_id = str(uuid.uuid4())

    # Check if this is the first broker (make it primary)
    count_result = await session.execute(
        text("SELECT COUNT(*) as cnt FROM user_broker_credentials WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    is_primary = count_result.scalar() == 0

    await session.execute(
        text(
            """
            INSERT INTO user_broker_credentials
                (id, tenant_id, broker, is_primary, api_key_encrypted, api_secret_encrypted,
                 client_id_encrypted, totp_secret_encrypted, access_token_encrypted,
                 token_expires_at, is_verified, created_at, updated_at)
            VALUES
                (:id, :tid, :broker, :is_primary, :api_key, :api_secret,
                 :client_id, :totp_secret, NULL,
                 NULL, :is_verified, :now, :now)
            """
        ),
        {
            "id": cred_id,
            "tid": tenant_id,
            "broker": body.broker,
            "is_primary": is_primary,
            "api_key": api_key_enc,
            "api_secret": api_secret_enc,
            "client_id": client_id_enc,
            "totp_secret": totp_secret_enc,
            "is_verified": is_verified,
            "now": now,
        },
    )

    await _audit_log(
        session, "BROKER_CREDENTIAL_STORED", tenant_id,
        {"broker": body.broker, "is_verified": is_verified},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    message = (
        f"Broker '{body.broker}' connected and verified successfully."
        if is_verified
        else f"Broker '{body.broker}' credentials stored but verification failed. Please check your credentials."
    )

    logger.info(
        "broker_connected",
        tenant_id=tenant_id,
        broker=body.broker,
        is_verified=is_verified,
    )
    return BrokerConnectResponse(broker=body.broker, is_verified=is_verified, message=message)


# ── GET /broker/status ───────────────────────────────────────────────────────


@router.get("/status", response_model=BrokerStatusResponse)
async def broker_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]

    result = await session.execute(
        text(
            """
            SELECT broker, is_verified, is_primary, token_expires_at, created_at
            FROM user_broker_credentials
            WHERE tenant_id = :tid
            ORDER BY created_at ASC
            """
        ),
        {"tid": tenant_id},
    )
    rows = result.fetchall()

    brokers = [
        BrokerStatusItem(
            broker=row.broker,
            is_verified=row.is_verified,
            is_primary=row.is_primary,
            token_expires_at=row.token_expires_at,
            connected_at=row.created_at,
        )
        for row in rows
    ]

    return BrokerStatusResponse(brokers=brokers)


# ── DELETE /broker/{broker} ──────────────────────────────────────────────────


@router.delete("/{broker}", response_model=BrokerDisconnectResponse)
async def disconnect_broker(
    broker: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if broker not in ("dhan", "zerodha"):
        return _error_response(400, "VALIDATION_ERROR", "Invalid broker. Must be 'dhan' or 'zerodha'.")

    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")

    result = await session.execute(
        text(
            "DELETE FROM user_broker_credentials WHERE tenant_id = :tid AND broker = :broker RETURNING id"
        ),
        {"tid": tenant_id, "broker": broker},
    )
    deleted = result.fetchone()

    if not deleted:
        return _error_response(404, "NOT_FOUND", f"Broker '{broker}' is not connected.")

    await _audit_log(
        session, "BROKER_CREDENTIAL_DELETED", tenant_id,
        {"broker": broker},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    logger.info("broker_disconnected", tenant_id=tenant_id, broker=broker)
    return BrokerDisconnectResponse(broker=broker, message=f"Broker '{broker}' disconnected and credentials purged.")


# ── POST /broker/{broker}/refresh ────────────────────────────────────────────


@router.post("/{broker}/refresh", response_model=BrokerRefreshResponse)
async def refresh_broker_token(
    broker: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if broker not in ("dhan", "zerodha"):
        return _error_response(400, "VALIDATION_ERROR", "Invalid broker. Must be 'dhan' or 'zerodha'.")

    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]

    result = await session.execute(
        text(
            """
            SELECT id, api_key_encrypted, api_secret_encrypted, client_id_encrypted, totp_secret_encrypted
            FROM user_broker_credentials
            WHERE tenant_id = :tid AND broker = :broker
            """
        ),
        {"tid": tenant_id, "broker": broker},
    )
    row = result.fetchone()

    if not row:
        return _error_response(404, "NOT_FOUND", f"Broker '{broker}' is not connected.")

    # Decrypt credentials for the refresh call
    api_key = decrypt(row.api_key_encrypted)
    api_secret = decrypt(row.api_secret_encrypted)
    client_id = decrypt(row.client_id_encrypted)
    totp_secret = decrypt(row.totp_secret_encrypted)

    await _audit_log(
        session, "BROKER_CREDENTIAL_DECRYPTED", tenant_id,
        {"broker": broker, "purpose": "token_refresh"},
    )

    # Attempt to refresh the broker access token
    new_token = None
    new_expiry = None

    try:
        if broker == "dhan":
            # Dhan uses TOTP-based re-authentication
            # In production, this would call Dhan's token generation endpoint
            # For now, we verify the credentials still work
            is_valid = await _verify_broker_credentials(broker, api_key, api_secret, client_id)
            if is_valid:
                new_token = api_key  # Dhan access token is the api_key itself
                new_expiry = datetime.now(timezone.utc)
            else:
                return _error_response(
                    502, "BROKER_ERROR",
                    f"Failed to refresh {broker} token. Credentials may be invalid.",
                )

        elif broker == "zerodha":
            # Zerodha requires a login flow; manual refresh triggers re-verification
            is_valid = await _verify_broker_credentials(broker, api_key, api_secret, client_id)
            if is_valid:
                new_expiry = datetime.now(timezone.utc)
            else:
                return _error_response(
                    502, "BROKER_ERROR",
                    f"Failed to refresh {broker} token. Re-login may be required.",
                )

    except Exception as exc:
        logger.error("broker_token_refresh_error", broker=broker, error=str(exc))
        return _error_response(502, "BROKER_ERROR", f"Error refreshing {broker} token: {str(exc)}")

    # Update token in DB (encrypted)
    update_params = {"id": row.id, "now": datetime.now(timezone.utc), "exp": new_expiry}
    if new_token:
        enc_token = encrypt(new_token)
        update_params["access_token"] = enc_token
        await session.execute(
            text(
                """
                UPDATE user_broker_credentials
                SET access_token_encrypted = :access_token, token_expires_at = :exp, updated_at = :now
                WHERE id = :id
                """
            ),
            update_params,
        )
    else:
        await session.execute(
            text(
                """
                UPDATE user_broker_credentials
                SET token_expires_at = :exp, updated_at = :now
                WHERE id = :id
                """
            ),
            update_params,
        )

    await session.commit()

    logger.info("broker_token_refreshed", tenant_id=tenant_id, broker=broker)
    return BrokerRefreshResponse(
        broker=broker,
        token_expires_at=new_expiry,
        message=f"Broker '{broker}' token refresh completed.",
    )
