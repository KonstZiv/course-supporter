"""FastAPI dependency injection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from course_supporter.auth.context import TenantContext
from course_supporter.auth.keys import hash_api_key
from course_supporter.llm.router import ModelRouter
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import APIKey, Tenant
from course_supporter.storage.s3 import S3Client

__all__ = ["get_current_tenant", "get_model_router", "get_s3_client", "get_session"]

api_key_header = APIKeyHeader(name="X-API-Key")


_get_session = Depends(get_session)


async def get_current_tenant(
    api_key: str = Security(api_key_header),
    session: AsyncSession = _get_session,
) -> TenantContext:
    """Authenticate request via API key, return tenant context.

    Raises:
        HTTPException 401: missing, invalid, inactive, or expired key.
    """
    key_hash = hash_api_key(api_key)

    stmt = (
        select(APIKey)
        .join(Tenant, APIKey.tenant_id == Tenant.id)
        .where(
            APIKey.key_hash == key_hash,
            APIKey.is_active.is_(True),
            Tenant.is_active.is_(True),
        )
        .options(selectinload(APIKey.tenant))
    )
    result = await session.execute(stmt)
    api_key_record = result.scalar_one_or_none()

    if api_key_record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if (
        api_key_record.expires_at is not None
        and api_key_record.expires_at < datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="API key expired")

    return TenantContext(
        tenant_id=api_key_record.tenant_id,
        tenant_name=api_key_record.tenant.name,
        scopes=api_key_record.scopes,
        rate_limit_prep=api_key_record.rate_limit_prep,
        rate_limit_check=api_key_record.rate_limit_check,
        key_prefix=api_key_record.key_prefix,
    )


async def get_model_router(request: Request) -> ModelRouter:
    """Retrieve ModelRouter from app state.

    Initialized during lifespan startup.
    """
    return cast(ModelRouter, request.app.state.model_router)


async def get_s3_client(request: Request) -> S3Client:
    """Retrieve S3Client from app state.

    Initialized during lifespan startup.
    """
    return cast(S3Client, request.app.state.s3_client)
