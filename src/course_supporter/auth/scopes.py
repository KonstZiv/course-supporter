"""Scope enforcement and rate limiting dependency factory."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.auth.rate_limiter import InMemoryRateLimiter

_tenant_dep = Depends(get_current_tenant)

# Global rate limiter instance (single-process; replace with Redis for scaling)
rate_limiter = InMemoryRateLimiter(window_seconds=60)


def require_scope(
    *required_scopes: str,
) -> Callable[..., Coroutine[Any, Any, TenantContext]]:
    """Dependency factory: require at least one scope, then enforce rate limit.

    Usage as parameter dependency (returns TenantContext)::

        async def endpoint(
            tenant: TenantContext = Depends(require_scope("prep")),
        ): ...

    Raises:
        HTTPException 403: if tenant has none of the required scopes.
        HTTPException 429: if rate limit exceeded (includes Retry-After header).
    """

    async def _check_scope(
        tenant: TenantContext = _tenant_dep,
    ) -> TenantContext:
        # 1. Scope check
        matched_scope: str | None = None
        for s in required_scopes:
            if s in tenant.scopes:
                matched_scope = s
                break
        if matched_scope is None:
            raise HTTPException(
                status_code=403,
                detail=f"Requires scope: {' or '.join(required_scopes)}",
            )

        # 2. Rate limit check
        limit = (
            tenant.rate_limit_prep
            if matched_scope == "prep"
            else tenant.rate_limit_check
        )
        key = f"{tenant.tenant_id}:{matched_scope}"
        allowed, retry_after = rate_limiter.check(key, limit)

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

        return tenant

    return _check_scope
