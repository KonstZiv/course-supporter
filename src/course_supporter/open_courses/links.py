"""The course key, the portal links of open courses, and the email mask.

The course key is a random URL-safe secret (``secrets.token_urlsafe``) of
:data:`ENROLL_KEY_BYTES` bytes — 128 bits; the author's link is
``{portal_base_url}/{tenant_id}/join/{key}``. Unlike a recovery token the key
is stored as is: the author reads the link back whenever they need it, and a
leaked link is closed by rotating the key.

Signup lifetimes are code constants (R9: unconfirmed requests live 7 days);
the human-readable phrases feed the ``{ttl}`` of the confirm email and MUST
stay in sync with the timedelta above them.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Final

from course_supporter.config import get_settings

ENROLL_KEY_BYTES: Final = 16
"""128 bits of randomness → a 22-character URL-safe key."""

SIGNUP_TTL: Final = timedelta(days=7)
"""An unconfirmed request is void this long after it was made (R9)."""

CONFIRMED_SIGNUP_RETENTION: Final = timedelta(days=30)
"""A confirmed request (with its password hash) is deleted this long after
it was made."""

_SIGNUP_TTL_DISPLAY: Final[dict[str, str]] = {"ukr": "7 днів", "eng": "7 days"}


def generate_enroll_key() -> str:
    """A fresh course key (never derived from anything)."""
    return secrets.token_urlsafe(ENROLL_KEY_BYTES)


def signup_ttl_display(language: str | None) -> str:
    """The confirm email's ``{ttl}`` phrase in the email's language."""
    return _SIGNUP_TTL_DISPLAY["eng" if language == "eng" else "ukr"]


def _portal_base() -> str:
    return get_settings().portal_base_url.rstrip("/")


def enrollment_url(tenant_id: uuid.UUID, key: str) -> str:
    """The course link the author gives out."""
    return f"{_portal_base()}/{tenant_id}/join/{key}"


def signup_confirm_url(tenant_id: uuid.UUID, raw_token: str) -> str:
    """The confirm link of a signup email (the token is URL-safe)."""
    return f"{_portal_base()}/{tenant_id}/join/confirm?token={raw_token}"


def author_login_url(tenant_id: uuid.UUID) -> str:
    """The author's own portal login page (DD-6-M)."""
    return f"{_portal_base()}/{tenant_id}/login"


def shared_login_url() -> str:
    """The shared login page: sign in by email and password."""
    return f"{_portal_base()}/login"


def mask_email(email: str) -> str:
    """``alice@example.com`` → ``a***@e***.com``: enough to recognise one's
    own address, too little to read someone else's (logs, responses)."""
    local, _, domain = email.partition("@")
    host, dot, tld = domain.rpartition(".")
    if not dot:
        host, tld = domain, ""
    masked_domain = f"{host[:1]}***" + (f".{tld}" if tld else "")
    return f"{local[:1]}***@{masked_domain}"
