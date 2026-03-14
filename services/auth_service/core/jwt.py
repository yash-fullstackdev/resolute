"""
JWT issuance and validation for auth_service.

Access token: HS256, 15-minute TTL, claims = {sub: tenant_id, email, tier, jti}
Refresh token: 256-bit random hex, stored in DB with tenant_id + expiry + revoked flag
JTI blacklist stored in Redis for instant revocation on logout.
"""

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(service="auth_service")

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_DAYS = 7

# Redis key prefix for JTI blacklist
JTI_BLACKLIST_PREFIX = "auth:jti:blacklist:"


def create_access_token(
    tenant_id: str,
    email: str,
    tier: str,
) -> tuple[str, str, datetime]:
    """
    Issue a new JWT access token.

    Returns:
        (encoded_token, jti, expires_at)
    """
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    expires_at = now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)

    payload = {
        "sub": tenant_id,
        "email": email,
        "tier": tier,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }

    token = pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    logger.info(
        "access_token_issued",
        tenant_id=tenant_id,
        jti=jti,
        expires_at=expires_at.isoformat(),
    )
    return token, jti, expires_at


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Returns the decoded claims dict.
    Raises jwt.ExpiredSignatureError, jwt.InvalidTokenError on failure.
    """
    payload = pyjwt.decode(
        token,
        JWT_SECRET,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["sub", "email", "tier", "jti", "exp", "iat"]},
    )
    return payload


async def is_jti_blacklisted(redis: Redis, jti: str) -> bool:
    """Check if a JTI has been blacklisted (token revoked)."""
    key = f"{JTI_BLACKLIST_PREFIX}{jti}"
    result = await redis.exists(key)
    return bool(result)


async def blacklist_jti(redis: Redis, jti: str, expires_at: datetime) -> None:
    """
    Add a JTI to the blacklist in Redis.

    TTL is set to the remaining lifetime of the access token so entries
    auto-expire once the token would have expired anyway.
    """
    key = f"{JTI_BLACKLIST_PREFIX}{jti}"
    now = datetime.now(timezone.utc)
    ttl_seconds = max(int((expires_at - now).total_seconds()), 1)
    await redis.setex(key, ttl_seconds, "1")

    logger.info(
        "jti_blacklisted",
        jti=jti,
        ttl_seconds=ttl_seconds,
    )


async def blacklist_jti_with_default_ttl(redis: Redis, jti: str) -> None:
    """
    Blacklist a JTI with the maximum possible remaining TTL (full access token lifetime).

    Used when we don't know the exact expiry (e.g., logout-all).
    """
    key = f"{JTI_BLACKLIST_PREFIX}{jti}"
    ttl_seconds = ACCESS_TOKEN_TTL_MINUTES * 60
    await redis.setex(key, ttl_seconds, "1")

    logger.info(
        "jti_blacklisted_default_ttl",
        jti=jti,
        ttl_seconds=ttl_seconds,
    )


def generate_refresh_token() -> str:
    """Generate a cryptographically secure 256-bit random hex refresh token."""
    return secrets.token_hex(32)  # 256 bits = 32 bytes = 64 hex chars


def refresh_token_expiry() -> datetime:
    """Calculate the expiry time for a new refresh token (7 days from now)."""
    return datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_TTL_DAYS)


async def validate_access_token(redis: Redis, token: str) -> dict:
    """
    Full validation of an access token: decode + check JTI blacklist.

    Returns decoded claims on success.
    Raises ValueError if token is blacklisted.
    Raises jwt.ExpiredSignatureError, jwt.InvalidTokenError on JWT failures.
    """
    claims = decode_access_token(token)
    jti = claims["jti"]

    if await is_jti_blacklisted(redis, jti):
        logger.warning(
            "blacklisted_token_used",
            jti=jti,
            tenant_id=claims.get("sub"),
        )
        raise ValueError("Token has been revoked")

    return claims
