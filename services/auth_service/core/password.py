"""
Password hashing and verification using bcrypt with cost factor 12.
"""

import bcrypt
import structlog

logger = structlog.get_logger(service="auth_service")

BCRYPT_COST = 12


def hash_password(password: str) -> str:
    """
    Hash a plaintext password using bcrypt with cost factor 12.

    Returns the encoded hash string suitable for database storage.
    """
    salt = bcrypt.gensalt(rounds=BCRYPT_COST)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    logger.debug("password_hashed", cost_factor=BCRYPT_COST)
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a plaintext password against a bcrypt hash.

    Returns True if the password matches, False otherwise.
    Uses constant-time comparison internally (via bcrypt).
    """
    try:
        result = bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
        return result
    except (ValueError, TypeError) as exc:
        logger.warning("password_verify_error", error=str(exc))
        return False
