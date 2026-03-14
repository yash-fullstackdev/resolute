"""
Auth router: registration, login, token refresh, logout, logout-all.

Rate limits (per IP):
  - POST /auth/register: 3/minute
  - POST /auth/login:    5/minute

Session management:
  - Max 5 concurrent sessions per user
  - Refresh token rotation on every refresh
  - Refresh token TTL = 7 days
"""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.jwt import (
    ACCESS_TOKEN_TTL_MINUTES,
    blacklist_jti,
    blacklist_jti_with_default_ttl,
    create_access_token,
    generate_refresh_token,
    refresh_token_expiry,
    validate_access_token,
)
from ..core.password import hash_password, verify_password
from ..db import get_session
from ..models.schemas import (
    ErrorDetail,
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    LogoutAllResponse,
    LogoutRequest,
    LogoutResponse,
    RegisterRequest,
    RegisterResponse,
    TokenRefreshRequest,
    TokenRefreshResponse,
)

logger = structlog.get_logger(service="auth_service")
router = APIRouter(prefix="/auth", tags=["auth"])

limiter = Limiter(key_func=get_remote_address)

MAX_SESSIONS_PER_USER = 5


def _error_response(status_code: int, code: str, message: str, details: dict | None = None):
    from fastapi.responses import JSONResponse

    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {}),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


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
    """Write an audit event to the audit_events table and structured log."""
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
                "details": __import__("json").dumps(details),
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


async def _enforce_session_limit(session: AsyncSession, tenant_id: str, redis) -> None:
    """
    If the tenant has >= MAX_SESSIONS_PER_USER active refresh tokens,
    revoke the oldest one and blacklist its last access token JTI.
    """
    result = await session.execute(
        text(
            """
            SELECT id, last_jti FROM refresh_tokens
            WHERE tenant_id = :tid AND revoked = false AND expires_at > NOW()
            ORDER BY created_at ASC
            """
        ),
        {"tid": tenant_id},
    )
    active_sessions = result.fetchall()

    if len(active_sessions) >= MAX_SESSIONS_PER_USER:
        # Revoke the oldest session(s) to make room
        sessions_to_revoke = active_sessions[: len(active_sessions) - MAX_SESSIONS_PER_USER + 1]
        for row in sessions_to_revoke:
            await session.execute(
                text("UPDATE refresh_tokens SET revoked = true WHERE id = :id"),
                {"id": row.id},
            )
            if row.last_jti:
                await blacklist_jti_with_default_ttl(redis, row.last_jti)

        logger.info(
            "session_limit_enforced",
            tenant_id=tenant_id,
            revoked_count=len(sessions_to_revoke),
        )


async def _get_redis(request: Request):
    """Extract Redis client from app state."""
    return request.app.state.redis


# ── POST /auth/register ──────────────────────────────────────────────────────


@router.post("/register", response_model=RegisterResponse, status_code=201)
@limiter.limit("3/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")

    # Check email uniqueness
    existing = await session.execute(
        text("SELECT id FROM tenants WHERE email = :email"),
        {"email": body.email},
    )
    if existing.fetchone():
        await _audit_log(
            session, "AUTH_REGISTER", None,
            {"email": body.email, "result": "email_exists"},
            ip_address=ip, user_agent=ua,
        )
        await session.commit()
        return _error_response(409, "VALIDATION_ERROR", "An account with this email already exists.")

    tenant_id = str(uuid.uuid4())
    pw_hash = hash_password(body.password)
    now = datetime.now(timezone.utc)
    trial_ends = now + timedelta(days=14)

    await session.execute(
        text(
            """
            INSERT INTO tenants (id, email, name, password_hash, subscription_tier,
                subscription_status, trial_ends_at, created_at, is_active, email_verified)
            VALUES (:id, :email, :name, :pw_hash, 'SIGNAL', 'TRIAL', :trial_ends,
                :created_at, true, false)
            """
        ),
        {
            "id": tenant_id,
            "email": body.email,
            "name": body.name,
            "pw_hash": pw_hash,
            "trial_ends": trial_ends,
            "created_at": now,
        },
    )

    await _audit_log(
        session, "AUTH_REGISTER", tenant_id,
        {"email": body.email, "result": "success"},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    logger.info("user_registered", tenant_id=tenant_id, email=body.email)
    return RegisterResponse(tenant_id=tenant_id, message="Registration successful. Check your email to verify.")


# ── POST /auth/login ─────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
):
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")
    redis = await _get_redis(request)

    # Fetch tenant by email
    result = await session.execute(
        text(
            """
            SELECT id, email, name, password_hash, subscription_tier,
                   subscription_status, is_active, email_verified
            FROM tenants WHERE email = :email
            """
        ),
        {"email": body.email},
    )
    tenant = result.fetchone()

    if not tenant:
        await _audit_log(
            session, "AUTH_LOGIN_FAILED", None,
            {"email": body.email, "reason": "user_not_found"},
            ip_address=ip, user_agent=ua,
        )
        await session.commit()
        return _error_response(401, "UNAUTHORIZED", "Invalid email or password.")

    if not verify_password(body.password, tenant.password_hash):
        await _audit_log(
            session, "AUTH_LOGIN_FAILED", tenant.id,
            {"email": body.email, "reason": "invalid_password"},
            ip_address=ip, user_agent=ua,
        )
        await session.commit()
        return _error_response(401, "UNAUTHORIZED", "Invalid email or password.")

    if not tenant.is_active:
        await _audit_log(
            session, "AUTH_LOGIN_FAILED", tenant.id,
            {"email": body.email, "reason": "account_inactive"},
            ip_address=ip, user_agent=ua,
        )
        await session.commit()
        return _error_response(403, "FORBIDDEN", "Account is deactivated. Contact support.")

    if not tenant.email_verified:
        await _audit_log(
            session, "AUTH_LOGIN_FAILED", tenant.id,
            {"email": body.email, "reason": "email_not_verified"},
            ip_address=ip, user_agent=ua,
        )
        await session.commit()
        return _error_response(403, "FORBIDDEN", "Please verify your email before logging in.")

    # Enforce session limit
    await _enforce_session_limit(session, tenant.id, redis)

    # Issue tokens
    access_token, jti, expires_at = create_access_token(
        tenant_id=tenant.id,
        email=tenant.email,
        tier=tenant.subscription_tier,
    )
    refresh_tok = generate_refresh_token()
    refresh_exp = refresh_token_expiry()

    # Store refresh token in DB
    await session.execute(
        text(
            """
            INSERT INTO refresh_tokens (id, tenant_id, token_hash, last_jti, expires_at, revoked, created_at)
            VALUES (:id, :tid, :token_hash, :jti, :exp, false, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": tenant.id,
            "token_hash": refresh_tok,
            "jti": jti,
            "exp": refresh_exp,
        },
    )

    await _audit_log(
        session, "AUTH_LOGIN_SUCCESS", tenant.id,
        {"email": tenant.email},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    logger.info("user_logged_in", tenant_id=tenant.id, email=tenant.email)
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_tok,
        tenant_id=tenant.id,
        tier=tenant.subscription_tier,
        expires_at=expires_at,
    )


# ── POST /auth/refresh ───────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenRefreshResponse)
async def refresh(
    request: Request,
    body: TokenRefreshRequest,
    session: AsyncSession = Depends(get_session),
):
    redis = await _get_redis(request)

    # Look up refresh token in DB
    result = await session.execute(
        text(
            """
            SELECT rt.id, rt.tenant_id, rt.last_jti, rt.expires_at, rt.revoked,
                   t.email, t.subscription_tier
            FROM refresh_tokens rt
            JOIN tenants t ON t.id = rt.tenant_id
            WHERE rt.token_hash = :token_hash
            """
        ),
        {"token_hash": body.refresh_token},
    )
    row = result.fetchone()

    if not row:
        return _error_response(401, "UNAUTHORIZED", "Invalid refresh token.")

    if row.revoked:
        # Potential token reuse — revoke ALL sessions for this tenant as a security measure
        logger.warning(
            "refresh_token_reuse_detected",
            tenant_id=row.tenant_id,
        )
        await session.execute(
            text("UPDATE refresh_tokens SET revoked = true WHERE tenant_id = :tid"),
            {"tid": row.tenant_id},
        )
        await session.commit()
        return _error_response(401, "UNAUTHORIZED", "Refresh token has been revoked. Please log in again.")

    now = datetime.now(timezone.utc)
    if row.expires_at.replace(tzinfo=timezone.utc) < now:
        return _error_response(401, "UNAUTHORIZED", "Refresh token has expired. Please log in again.")

    # Revoke old refresh token (rotation)
    await session.execute(
        text("UPDATE refresh_tokens SET revoked = true WHERE id = :id"),
        {"id": row.id},
    )

    # Blacklist old JTI if present
    if row.last_jti:
        old_expires = now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)
        await blacklist_jti(redis, row.last_jti, old_expires)

    # Issue new tokens
    access_token, new_jti, expires_at = create_access_token(
        tenant_id=row.tenant_id,
        email=row.email,
        tier=row.subscription_tier,
    )
    new_refresh = generate_refresh_token()
    new_refresh_exp = refresh_token_expiry()

    await session.execute(
        text(
            """
            INSERT INTO refresh_tokens (id, tenant_id, token_hash, last_jti, expires_at, revoked, created_at)
            VALUES (:id, :tid, :token_hash, :jti, :exp, false, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": row.tenant_id,
            "token_hash": new_refresh,
            "jti": new_jti,
            "exp": new_refresh_exp,
        },
    )
    await session.commit()

    logger.info("token_refreshed", tenant_id=row.tenant_id)
    return TokenRefreshResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_at=expires_at,
    )


# ── POST /auth/logout ────────────────────────────────────────────────────────


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    body: LogoutRequest,
    session: AsyncSession = Depends(get_session),
):
    redis = await _get_redis(request)
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")

    # Find and revoke the refresh token
    result = await session.execute(
        text(
            """
            SELECT id, tenant_id, last_jti, expires_at
            FROM refresh_tokens
            WHERE token_hash = :token_hash AND revoked = false
            """
        ),
        {"token_hash": body.refresh_token},
    )
    row = result.fetchone()

    if not row:
        return _error_response(401, "UNAUTHORIZED", "Invalid or already revoked refresh token.")

    # Revoke refresh token
    await session.execute(
        text("UPDATE refresh_tokens SET revoked = true WHERE id = :id"),
        {"id": row.id},
    )

    # Blacklist the last-issued JTI
    if row.last_jti:
        now = datetime.now(timezone.utc)
        token_expires = now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)
        await blacklist_jti(redis, row.last_jti, token_expires)

    await _audit_log(
        session, "AUTH_LOGOUT", row.tenant_id,
        {"session_id": row.id},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    logger.info("user_logged_out", tenant_id=row.tenant_id)
    return LogoutResponse(message="Successfully logged out.")


# ── POST /auth/logout-all ────────────────────────────────────────────────────


@router.post("/logout-all", response_model=LogoutAllResponse)
async def logout_all(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    redis = await _get_redis(request)
    ip = _client_ip(request)
    ua = request.headers.get("User-Agent", "")

    # Extract tenant_id from the Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid Authorization header.")

    token = auth_header[7:]
    try:
        claims = await validate_access_token(redis, token)
    except Exception:
        return _error_response(401, "UNAUTHORIZED", "Invalid or expired access token.")

    tenant_id = claims["sub"]

    # Get all active sessions and their JTIs
    result = await session.execute(
        text(
            """
            SELECT id, last_jti FROM refresh_tokens
            WHERE tenant_id = :tid AND revoked = false
            """
        ),
        {"tid": tenant_id},
    )
    active_sessions = result.fetchall()

    # Revoke all refresh tokens
    await session.execute(
        text("UPDATE refresh_tokens SET revoked = true WHERE tenant_id = :tid AND revoked = false"),
        {"tid": tenant_id},
    )

    # Blacklist all known JTIs
    for row in active_sessions:
        if row.last_jti:
            await blacklist_jti_with_default_ttl(redis, row.last_jti)

    revoked_count = len(active_sessions)

    await _audit_log(
        session, "AUTH_LOGOUT", tenant_id,
        {"action": "logout_all", "sessions_revoked": revoked_count},
        ip_address=ip, user_agent=ua,
    )
    await session.commit()

    logger.info("user_logged_out_all", tenant_id=tenant_id, sessions_revoked=revoked_count)
    return LogoutAllResponse(
        message="All sessions terminated.",
        sessions_revoked=revoked_count,
    )
