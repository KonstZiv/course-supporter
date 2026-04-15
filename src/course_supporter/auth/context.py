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
