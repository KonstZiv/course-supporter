"""Storage management API endpoints.

Provides tenant-scoped file listing, usage tracking, and deletion.

Routes
------
- ``GET    /storage/files``        — List tenant's files in S3
- ``GET    /storage/usage``        — Total storage used by tenant
- ``DELETE /storage/files/{key:path}`` — Delete file: KD3 soft-delete the
  AuthoredDocument referencing the URL when one exists; otherwise force-
  orphan-cleanup mode enqueues the single key for ARQ-deferred S3 delete.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import StorageFileResponse, StorageUsageResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.cascade import CascadeDeleteService, build_cascade_map
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

router = APIRouter(tags=["storage"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
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
    arq: ArqDep,
) -> None:
    """KD3 soft-delete the matching AuthoredDocument + ARQ-deferred S3 cleanup.

    Two-mode dispatch (vision §3 KD3 + §6 QQ5; mirrors commit (k)
    ``delete_node`` / commit (l) ``delete_document`` pattern with cascade
    rooted at AuthoredDocument plus a force-orphan fallback for keys with
    no DB row):

    - **Mode A — with DB row.** ``get_by_source_url`` resolves to an
      ``AuthoredDocument``; cascade soft-delete dispatches the four-
      phase hook chain (cancel → invalidate → scrub → write deleted_at
      → flush per cascade engine invariant). ``on_cancel_jobs`` flips
      active Jobs scoped to the document's parent CourseNode to
      ``status='cancelled'`` per vision §KD13 (closure-augmented with
      ``course_node_id`` since cascade victim ids are document-keyed —
      mirrors ``delete_document``). ``on_invalidate_hashes`` cascade
      hook bridges :class:`ContentHashService.invalidate_subtree` so
      parent-chain hash recompute treats victims as already gone
      (Gap 3 from commit (i)). Class-level ``__scrub_callable__``
      clears ``filename`` + ``source_url`` (KD3 marker per hotfix-4).
      DocumentSummary + DocumentSegment cascade chain is no-op in
      Phase 1 per Amendment 16.
    - **Mode B — force-orphan.** No ``AuthoredDocument`` references the
      URL (e.g. an upload that crashed before the row landed, or a
      manually placed file). No cascade work; ``enqueue_s3_cleanup``
      with the single key. Helper still creates a ``Job`` row carrying
      the tenant scope so cost-attribution + reactivate-flow remain
      eligible per KD13.

    Both modes route the actual S3 ``DeleteObject`` call through the
    ARQ ``s3_cleanup_task`` worker (probe §1.7 idempotent on missing).
    Behavior shift vs the legacy synchronous ``s3.delete_object``
    in-handler: deletion becomes eventually-consistent post-202-style
    handoff. Intended per PHASE.md §2.2 sub-area ``kd3`` third bullet —
    same QQ5 ordering invariant the other two KD3 handlers obey.

    Tenant isolation gate (preserved from legacy): caller's tenant
    prefix MUST match the key prefix; cross-tenant requests yield
    HTTP 403 BEFORE any DB lookup or cascade work.
    """
    expected_prefix = _tenant_prefix(tenant.tenant_id)
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Key does not belong to tenant.")

    # Look up the AuthoredDocument (if any) referencing this S3 URL.
    s3_url = f"{s3._endpoint_url}/{s3._bucket}/{key}"
    document_repo = AuthoredDocumentRepository(session)
    document = await document_repo.get_by_source_url(s3_url)

    if document is not None:
        # Mode A — with DB row. Cascade soft-delete + s3_cleanup enqueue.
        course_node_id = document.course_node_id
        document_id = document.id

        content_hash_service = ContentHashService(session)
        job_cancellation_service = JobCancellationService(session)

        async def invalidate_hook(
            ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
        ) -> None:
            await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

        async def cancel_hook(victim_ids: list[uuid.UUID]) -> None:
            # KD13 closure-augmentation: cascade victim ids are
            # ``[document_id]`` but JCS lookup paths are
            # course_node_id-keyed; inject ``course_node_id`` so the
            # Job.course_node_id path matches node-scoped ingestion
            # jobs. Mirrors the delete_document handler at
            # ``api/routes/documents.py``.
            augmented = [*victim_ids, course_node_id]
            await job_cancellation_service.cancel_jobs_for_entities(augmented)

        cascade_service = CascadeDeleteService(session)
        cascade_map = build_cascade_map(AuthoredDocument)
        await cascade_service.soft_delete_with_cascade(
            document,
            cascade_map,
            on_cancel_jobs=cancel_hook,
            on_invalidate_hashes=invalidate_hook,
        )

        await enqueue_s3_cleanup(
            session=session,
            arq=arq,
            file_keys=[key],
            tenant_id=tenant.tenant_id,
            course_node_id=course_node_id,
        )

        logger.info(
            "storage_file_deleted_with_cascade",
            key=key,
            document_id=str(document_id),
            course_node_id=str(course_node_id),
        )
        return

    # Mode B — force-orphan. No DB row to attribute; helper still
    # creates a tenant-scoped Job for cost + reactivate eligibility.
    await enqueue_s3_cleanup(
        session=session,
        arq=arq,
        file_keys=[key],
        tenant_id=tenant.tenant_id,
    )
    logger.info("storage_file_orphan_cleanup_enqueued", key=key)
