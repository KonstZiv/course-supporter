"""Storage management API endpoints.

Provides tenant-scoped file listing, usage tracking, and deletion.

Routes
------
- ``GET    /storage/files``        — List tenant's files in S3
- ``GET    /storage/usage``        — Total storage used by tenant
- ``DELETE /storage/files/{key:path}`` — Delete file from S3 + cascade
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_s3_client, get_session
from course_supporter.api.schemas import StorageFileResponse, StorageUsageResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.storage.material_entry_repository import MaterialEntryRepository
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

router = APIRouter(tags=["storage"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


def _tenant_prefix(tenant_id: object) -> str:
    return f"tenants/{tenant_id}/"


@router.get("/storage/files")
async def list_files(
    tenant: SharedDep,
    s3: S3Dep,
) -> list[StorageFileResponse]:
    """List all files in tenant's S3 storage."""
    prefix = _tenant_prefix(tenant.tenant_id)
    objects = await s3.list_objects(prefix)
    return [
        StorageFileResponse(
            key=obj["key"],
            size_bytes=obj["size"],
            last_modified=obj["last_modified"],
        )
        for obj in objects
    ]


@router.get("/storage/usage")
async def get_usage(
    tenant: SharedDep,
    s3: S3Dep,
) -> StorageUsageResponse:
    """Get total storage usage for the tenant."""
    prefix = _tenant_prefix(tenant.tenant_id)
    total_bytes, file_count = await s3.get_usage(prefix)
    return StorageUsageResponse(
        total_bytes=total_bytes,
        file_count=file_count,
    )


@router.delete("/storage/files/{key:path}", status_code=204)
async def delete_file(
    key: str,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
) -> None:
    """Delete a file from S3 and cascade to MaterialEntry.

    Verifies the key belongs to the tenant before deletion.
    """
    expected_prefix = _tenant_prefix(tenant.tenant_id)
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Key does not belong to tenant.")

    # Delete from S3
    await s3.delete_object(key)

    # Cascade: find and delete MaterialEntry referencing this key
    s3_url = f"{s3._endpoint_url}/{s3._bucket}/{key}"
    entry_repo = MaterialEntryRepository(session)
    entry = await entry_repo.get_by_source_url(s3_url)
    if entry is not None:
        await entry_repo.delete(entry.id)
        await session.commit()
        logger.info(
            "storage_file_deleted_with_cascade",
            key=key,
            entry_id=str(entry.id),
        )
    else:
        logger.info("storage_file_deleted", key=key)
