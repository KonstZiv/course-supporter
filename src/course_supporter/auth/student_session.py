"""HS256 stateless bearer session tokens for the student portal (Phase 6 T1).

The token carries ``student_id`` + ``tenant_id`` + expiry — it is NOT a server
session record. Revocation is immediate regardless of TTL because
``get_current_student`` re-checks the credential's ``is_active`` on every
request (mirror of the API-key path), so no session table is needed. Secret +
TTL come from settings (KD17 ratify Q1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from course_supporter.config import get_settings

_ALGORITHM = "HS256"


class SessionTokenError(Exception):
    """Raised when a session token is missing, malformed, or expired."""


def issue_session_token(
    *,
    student_id: uuid.UUID,
    tenant_id: uuid.UUID,
    now: datetime | None = None,
) -> str:
    """Issue a signed bearer token for a student session.

    Args:
        student_id: Authenticated student.
        tenant_id: Owning tenant (the portal is tenant-scoped).
        now: Override for the issue time (testing).

    Returns:
        A compact HS256 JWT string.
    """
    settings = get_settings()
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(hours=settings.portal_session_ttl_hours)
    payload = {
        "sub": str(student_id),
        "tid": str(tenant_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(
        payload,
        settings.portal_session_secret.get_secret_value(),
        algorithm=_ALGORITHM,
    )


def decode_session_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Verify and decode a session token.

    Returns:
        ``(student_id, tenant_id)``.

    Raises:
        SessionTokenError: if the token is invalid, expired, or malformed.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.portal_session_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
        )
    except jwt.PyJWTError as exc:
        raise SessionTokenError(str(exc)) from exc

    try:
        return uuid.UUID(payload["sub"]), uuid.UUID(payload["tid"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SessionTokenError("malformed session token payload") from exc
