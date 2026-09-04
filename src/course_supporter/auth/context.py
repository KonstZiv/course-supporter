"""Authenticated tenant context for request processing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    """Authenticated tenant context, injected into every request.

    Extracted from API key during authentication. Rate limits are not
    stored here — they are resolved from the auth registry using
    ``plan_id`` at the moment a scope is enforced.
    """

    tenant_id: uuid.UUID
    tenant_name: str
    scopes: list[str]
    plan_id: str
    key_prefix: str


@dataclass(frozen=True)
class StudentContext:
    """Authenticated student-portal context (Phase 6 T1, KD17).

    Mirror of :class:`TenantContext` for the native session path. Resolved
    from the bearer token's ``student_id`` and validated against the live
    credential (``is_active``) on every request, so access revocation is
    immediate. The portal is tenant-scoped — ``tenant_id`` comes from the
    credential, not a request field.
    """

    student_id: uuid.UUID
    tenant_id: uuid.UUID
    login: str
    display_name: str | None
    # The language this student last asked their review in (ISO 639-3), or
    # None. Carried on the context rather than read again in the one route
    # that serves it: the resolver already loads the whole ``Student`` row to
    # authenticate, so this is a field that was being discarded, not a query.
    # Defaulted so the three route-test stubs that build a context by hand
    # keep working without knowing about a field they do not exercise.
    preferred_language: str | None = None
