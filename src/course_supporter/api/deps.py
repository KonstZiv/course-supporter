"""FastAPI dependency injection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPBearer
from fastapi.security.http import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from course_supporter.auth.context import StudentContext, TenantContext
from course_supporter.auth.keys import hash_api_key
from course_supporter.auth.student_session import (
    SessionTokenError,
    decode_session_token,
)
from course_supporter.llm.router import ModelRouter
from course_supporter.llm.stage_router import StageRouter
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import APIKey, Student, StudentCredential, Tenant
from course_supporter.storage.s3 import S3Client

if TYPE_CHECKING:
    from arq.connections import ArqRedis

__all__ = [
    "get_arq_redis",
    "get_current_student",
    "get_current_tenant",
    "get_model_router",
    "get_s3_client",
    "get_session",
    "get_stage_router",
]

api_key_header = APIKeyHeader(name="X-API-Key")

# Portal session bearer scheme (Phase 6 T1). auto_error=False so a missing or
# non-bearer Authorization header yields None (handled as 401 below) rather
# than FastAPI's default 403, keeping the unauthenticated response a clean 401.
bearer_scheme = HTTPBearer(auto_error=False)
_bearer_dep = Security(bearer_scheme)


_get_session = Depends(get_session)


async def get_current_tenant(
    api_key: str = Security(api_key_header),
    session: AsyncSession = _get_session,
) -> TenantContext:
    """Authenticate request via API key, return tenant context.

    Raises:
        HTTPException 401: missing, invalid, or inactive key
            (detail: "Invalid API key"), or expired key
            (detail: "API key expired").
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
        plan_id=api_key_record.plan_id,
        key_prefix=api_key_record.key_prefix,
    )


async def get_current_student(
    credentials: HTTPAuthorizationCredentials | None = _bearer_dep,
    session: AsyncSession = _get_session,
) -> StudentContext:
    """Authenticate a portal request via its bearer session token (KD17).

    Decodes the stateless token, then validates it against the live
    credential on every request — so revoking access (``is_active = False``)
    rejects existing tokens immediately, without a session table. Mirrors
    ``get_current_tenant``'s per-request active check.

    Raises:
        HTTPException 401: missing/invalid/expired token, or a credential that
            no longer exists, is inactive, or whose tenant/student is inactive.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        student_id, tenant_id = decode_session_token(credentials.credentials)
    except SessionTokenError as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired session"
        ) from exc

    stmt = (
        select(StudentCredential, Student)
        .join(Student, StudentCredential.student_id == Student.id)
        .join(Tenant, StudentCredential.tenant_id == Tenant.id)
        .where(
            StudentCredential.student_id == student_id,
            StudentCredential.tenant_id == tenant_id,
            StudentCredential.is_active.is_(True),
            Tenant.is_active.is_(True),
            Student.deleted_at.is_(None),
        )
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        raise HTTPException(status_code=401, detail="Session is no longer valid")

    credential, student = row
    return StudentContext(
        student_id=student.id,
        tenant_id=credential.tenant_id,
        login=credential.login,
        display_name=student.display_name,
    )


async def get_arq_redis(request: Request) -> ArqRedis:
    """Retrieve ARQ Redis pool from app state.

    Initialized during lifespan startup.
    """
    return cast("ArqRedis", request.app.state.arq_redis)


async def get_model_router(request: Request) -> ModelRouter:
    """Retrieve ModelRouter from app state.

    Initialized during lifespan startup.
    """
    return cast(ModelRouter, request.app.state.model_router)


async def get_stage_router(request: Request) -> StageRouter:
    """Retrieve StageRouter from app state.

    Initialized during lifespan startup.
    """
    return cast(StageRouter, request.app.state.stage_router)


async def get_s3_client(request: Request) -> S3Client:
    """Retrieve S3Client from app state.

    Initialized during lifespan startup.
    """
    return cast(S3Client, request.app.state.s3_client)
