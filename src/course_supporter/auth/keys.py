"""API key generation and hashing utilities."""

from __future__ import annotations

import hashlib
import secrets


def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    """Generate API key, return (full_key, key_hash, key_prefix).

    Full key is shown only once at creation time.
    Only hash and prefix are stored in DB.

    Args:
        environment: Key environment, typically 'live' or 'test'.

    Returns:
        Tuple of (full_key, key_hash, key_prefix).
    """
    random_part = secrets.token_hex(16)
    full_key = f"cs_{environment}_{random_part}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = f"cs_{environment}_{random_part[:4]}"
    return full_key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    """Hash an API key for lookup.

    Args:
        key: The full API key string.

    Returns:
        SHA-256 hex digest of the key.
    """
    return hashlib.sha256(key.encode()).hexdigest()
