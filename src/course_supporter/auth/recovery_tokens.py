"""Recovery-token constants and helpers (Phase 6 R2, password-recovery).

Backs the single-use ``student_credential_tokens`` flows. The raw token is a
URL-safe secret embedded in the recovery email; only its SHA-256 hash
(``hash_api_key`` — the fast unsalted API-key hasher, NOT the slow argon2
password hasher) is stored, so a DB read cannot reconstruct a live token.

TTLs are code constants (Q8: env carries deploy-specifics only). Reset is
short-lived (a password change is security-sensitive); email-confirm is longer
(the student may not read mail immediately). The human-readable TTL phrases
below feed the ``{ttl}`` email placeholder and MUST stay in sync with the
timedeltas above them.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Final

from course_supporter.auth.keys import hash_api_key

# Token purposes — must match the student_credential_tokens.purpose CHECK, the
# email_text.py message ids, and the StudentCredentialToken model.
PURPOSE_PASSWORD_RESET: Final = "password_reset"  # noqa: S105 — purpose id, not a secret
PURPOSE_EMAIL_CONFIRM: Final = "email_confirm"

# Single-use token TTLs (code constants — Q8).
PASSWORD_RESET_TTL: Final = timedelta(minutes=30)
EMAIL_CONFIRM_TTL: Final = timedelta(hours=24)

_TTL: Final[dict[str, timedelta]] = {
    PURPOSE_PASSWORD_RESET: PASSWORD_RESET_TTL,
    PURPOSE_EMAIL_CONFIRM: EMAIL_CONFIRM_TTL,
}

# Human-readable Ukrainian TTL phrases for the email {ttl} placeholder — kept in
# sync with the timedeltas above ("Посилання дійсне {ttl}").
_TTL_DISPLAY: Final[dict[str, str]] = {
    PURPOSE_PASSWORD_RESET: "30 хвилин",
    PURPOSE_EMAIL_CONFIRM: "24 години",
}

# URL-safe secret byte count → a ~43-char raw token; its SHA-256 hex is always
# 64 chars, matching the token_hash String(64) column.
_TOKEN_BYTES: Final = 32


def generate_token() -> str:
    """Generate a fresh URL-safe raw recovery token (never stored)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """Hash a raw token for at-rest storage / redemption lookup (SHA-256)."""
    return hash_api_key(raw_token)


def ttl_for(purpose: str) -> timedelta:
    """Return the TTL for a token ``purpose``."""
    return _TTL[purpose]


def ttl_display(purpose: str) -> str:
    """Return the Ukrainian TTL phrase for a purpose's email ``{ttl}``."""
    return _TTL_DISPLAY[purpose]
