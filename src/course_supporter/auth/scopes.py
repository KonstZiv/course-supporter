"""Scope enforcement dependency factory."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext

_tenant_dep = Depends(get_current_tenant)


def require_scope(
    *required_scopes: str,
) -> Callable[..., Coroutine[Any, Any, TenantContext]]:
    """Dependency factory: require at least one of the given scopes.

    Usage as parameter dependency (returns TenantContext)::

        async def endpoint(
            tenant: TenantContext = Depends(require_scope("prep")),
        ): ...

    Raises:
        HTTPException 403: if tenant has none of the required scopes.
    """

    async def _check_scope(
        tenant: TenantContext = _tenant_dep,
    ) -> TenantContext:
        if not any(s in tenant.scopes for s in required_scopes):
            raise HTTPException(
                status_code=403,
                detail=f"Requires scope: {' or '.join(required_scopes)}",
            )
        return tenant

    return _check_scope
