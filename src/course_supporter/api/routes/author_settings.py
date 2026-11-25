"""The author's own settings kept by the server (V1).

Routes
------
- ``GET /author/interface-language`` — the language of the author panel
  pages the author picked, or null until they pick one.
- ``PUT /author/interface-language`` — the author picks it; it is kept on
  the author's record (``tenants.interface_language``), so the panel opens
  in it after login on any device.

The student's counterpart is M-3 (``/portal/me/interface-language``). Both
routes take the tenant's key with scope ``prep``; the tenant comes from the
key, never from the body. Nothing else on the server reads the column.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.schemas import (
    AuthorInterfaceLanguageRequest,
    AuthorInterfaceLanguageResponse,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.orm import Tenant

logger = structlog.get_logger()

router = APIRouter(tags=["author"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope("prep"))]

_PATH = "/author/interface-language"


async def _stored_language(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """The tenant's stored panel language; the key already proved the tenant."""
    stmt = select(Tenant.interface_language).where(Tenant.id == tenant_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@router.get(_PATH, response_model=AuthorInterfaceLanguageResponse)
async def get_interface_language(
    tenant: PrepDep,
    session: SessionDep,
) -> AuthorInterfaceLanguageResponse:
    """Serve the language of the author panel pages the author picked (V1)."""
    language = await _stored_language(session, tenant.tenant_id)
    return AuthorInterfaceLanguageResponse(interface_language=language)


@router.put(_PATH, response_model=AuthorInterfaceLanguageResponse)
async def set_interface_language(
    body: AuthorInterfaceLanguageRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> AuthorInterfaceLanguageResponse:
    """Keep the language of the author panel pages the author picked (V1).

    Only the tenant of the key is changed. The language arrives already
    canonical (the schema validates it), and an unchanged choice costs no
    write.
    """
    if await _stored_language(session, tenant.tenant_id) != body.language:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant.tenant_id)
            .values(interface_language=body.language)
        )
        await session.commit()
        logger.info(
            "tenant_interface_language_updated",
            tenant_id=str(tenant.tenant_id),
            language=body.language,
        )
    return AuthorInterfaceLanguageResponse(interface_language=body.language)
