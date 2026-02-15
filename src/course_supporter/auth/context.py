"""Authenticated tenant context for request processing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    """Authenticated tenant context, injected into every request.

    Extracted from API key during authentication.
    """

    tenant_id: uuid.UUID
    tenant_name: str
    scopes: list[str]
    rate_limit_prep: int
    rate_limit_check: int
    key_prefix: str
