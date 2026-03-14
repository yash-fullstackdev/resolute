"""
AES-256-GCM encryption/decryption with key versioning for broker credential vault.

Encrypted payload format: version_byte(1) + nonce(12) + ciphertext(N)
"""

import os
import secrets

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger(service="auth_service")


def _load_key_versions() -> dict[int, bytes]:
    """Load all available key versions from environment variables."""
    versions: dict[int, bytes] = {}
    v1_hex = os.environ.get("CREDENTIAL_MASTER_KEY_V1", "")
    if v1_hex:
        versions[1] = bytes.fromhex(v1_hex)
    v2_hex = os.environ.get("CREDENTIAL_MASTER_KEY", "")
    if v2_hex:
        versions[2] = bytes.fromhex(v2_hex)
    return versions


def _get_key_versions() -> dict[int, bytes]:
    """Return cached key versions, loading from env on first call."""
    if not hasattr(_get_key_versions, "_cache"):
        _get_key_versions._cache = _load_key_versions()
    return _get_key_versions._cache


def reload_keys() -> None:
    """Force reload of encryption keys from environment (used on startup or rotation)."""
    if hasattr(_get_key_versions, "_cache"):
        del _get_key_versions._cache


CURRENT_KEY_VERSION = 2


def encrypt(plaintext: str) -> bytes:
    """
    AES-256-GCM encrypt with key versioning.

    Returns: version_byte(1) + nonce(12) + ciphertext(N)
    """
    key_versions = _get_key_versions()
    key = key_versions.get(CURRENT_KEY_VERSION)
    if key is None:
        raise RuntimeError(
            f"Encryption key version {CURRENT_KEY_VERSION} not configured. "
            "Set CREDENTIAL_MASTER_KEY environment variable."
        )
    if len(key) != 32:
        raise RuntimeError("CREDENTIAL_MASTER_KEY must be exactly 32 bytes (256 bits).")

    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)  # 96-bit nonce
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = bytes([CURRENT_KEY_VERSION]) + nonce + ciphertext

    logger.debug(
        "credential_encrypted",
        key_version=CURRENT_KEY_VERSION,
        payload_size=len(payload),
    )
    return payload


def decrypt(token: bytes) -> str:
    """
    AES-256-GCM decrypt with key versioning.

    token format: version_byte(1) + nonce(12) + ciphertext(N)
    """
    if len(token) < 14:
        raise ValueError("Encrypted token too short — expected at least 14 bytes (version + nonce + data).")

    version = token[0]
    key_versions = _get_key_versions()
    key = key_versions.get(version)

    if key is None:
        raise RuntimeError(
            f"Decryption key version {version} not available. "
            "Ensure the corresponding key is configured in environment."
        )

    aesgcm = AESGCM(key)
    nonce = token[1:13]
    ciphertext = token[13:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")

    logger.debug(
        "credential_decrypted",
        key_version=version,
    )
    return plaintext


def needs_reencryption(token: bytes) -> bool:
    """Check if an encrypted token uses an old key version and needs re-encryption."""
    if len(token) < 1:
        return False
    return token[0] != CURRENT_KEY_VERSION


def reencrypt(token: bytes) -> bytes:
    """Decrypt with old key and re-encrypt with current key version."""
    plaintext = decrypt(token)
    return encrypt(plaintext)
