"""Argon2id password hashing for student-portal credentials (Phase 6 T1).

API keys use a fast, unsalted SHA-256 (``auth/keys.py``) — appropriate for
high-entropy random tokens. Student passwords are low-entropy human secrets
and need a slow, salted KDF: argon2id via ``argon2-cffi``. This module is the
single point for hashing and verifying portal passwords plus the
minimum-length policy (NIST: length over forced composition).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Minimum password length (KD17 ratify Q3). Length floor only — no forced
# composition rules, per current NIST guidance.
MIN_PASSWORD_LENGTH = 10

# Argon2id with argon2-cffi defaults (id type, sensible time/memory cost).
_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """Raised when a password fails the minimum-length policy."""


def validate_password_length(password: str) -> None:
    """Raise :class:`WeakPasswordError` if the password is too short."""
    if len(password) < MIN_PASSWORD_LENGTH:
        msg = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        raise WeakPasswordError(msg)


def hash_password(password: str) -> str:
    """Validate length, then return an argon2id hash.

    Raises:
        WeakPasswordError: if the password is shorter than
            ``MIN_PASSWORD_LENGTH``.
    """
    validate_password_length(password)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return ``True`` iff ``password`` matches ``password_hash``.

    Never raises on a wrong password or a malformed stored hash — both map to
    ``False`` so callers branch on a plain boolean.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False
