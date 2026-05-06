"""Authored document management API endpoints.

Provides CRUD operations for authored documents attached to course
tree nodes. Each document goes through a lifecycle (RAW → PENDING →
READY/ERROR) tracked via the derived ``state`` property. Ingestion
is auto-enqueued on creation and can be retried on failure.

Tenant isolation is enforced by verifying node ownership via tenant_id.

Routes
------
- ``POST   /nodes/{nid}/documents``                — Add document to node
- ``POST   /nodes/{nid}/documents/upload-url``     — Get presigned upload URL
- ``POST   /nodes/{nid}/documents/confirm-upload`` — Confirm presigned upload
- ``GET    /nodes/{nid}/documents``                — List documents for node
- ``GET    /documents/{did}``                      — Get single document
- ``PATCH  /documents/{did}``                      — Update document
- ``DELETE /documents/{did}``                      — Delete document (KD3 cascade)
- ``POST   /documents/{did}/retry``                — Retry failed ingestion
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    AuthoredDocumentCreateResponse,
    AuthoredDocumentResponse,
    AuthoredDocumentUpdateRequest,
    ConfirmUploadRequest,
    PresignedUrlRequest,
    PresignedUrlResponse,
)
from course_supporter.api.upload_validation import (
    ALLOWED_EXTENSIONS,
    check_platform,
    file_extension,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_ingestion
from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.models.methodist import AssignmentType
from course_supporter.models.source import MaterialRole, SourceType
from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.cascade import CascadeDeleteService, build_cascade_map
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.storage.s3 import S3Client, upload_file_chunks

logger = structlog.get_logger()

router = APIRouter(tags=["documents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the tenant."""
    repo = CourseNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


async def _require_document_for_tenant(
    document_repo: AuthoredDocumentRepository,
    node_repo: CourseNodeRepository,
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> AuthoredDocument:
    """Verify the document exists and belongs to the tenant.

    Checks AuthoredDocument → CourseNode → tenant_id chain.
    """
    document = await document_repo.get_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")

    node = await node_repo.get_by_id(document.course_node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.post("/nodes/{node_id}/documents", status_code=201)
async def create_document(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    source_type: Annotated[
        SourceType,
        Form(description="Document type: video, presentation, text, or web."),
    ],
    material_role: Annotated[
        MaterialRole,
        Form(
            description="Role: educational (delivers content) "
            "or methodological (declares course intent).",
        ),
    ] = MaterialRole.EDUCATIONAL,
    task_type: Annotated[
        AssignmentType | None,
        Form(
            description=(
                "Mark the document as a concrete task of the given taxonomy "
                "tier (test, short_task, task, project). Omit for regular "
                "documents."
            ),
        ),
    ] = None,
    source_url: Annotated[
        str | None,
        Form(description="URL to the source document. Required if no file."),
    ] = None,
    file: Annotated[
        UploadFile | None,
        File(
            description=(
                "File upload (multipart). Accepted formats: "
                "presentation (pdf, pptx), text (md, txt, docx, html), "
                "video (mp4, webm, mkv, avi). "
                "Required if source_url is not provided."
            ),
        ),
    ] = None,
    filename: Annotated[
        str | None,
        Form(description="Override filename (optional, defaults to uploaded name)."),
    ] = None,
    language: Annotated[
        str | None,
        Form(
            description=(
                "Optional ISO 639-1 language override. When empty, the course "
                "default is used and STT falls back to auto-detection."
            ),
            pattern=r"^[a-z]{2}$",
        ),
    ] = None,
) -> AuthoredDocumentCreateResponse:
    """Add a new authored document to a tree node.

    Accepts either a URL or a file upload. If a file is provided,
    it is uploaded to S3/MinIO and the resulting URL is stored.

    Creates an ``AuthoredDocument`` and auto-enqueues an ingestion job
    via ARQ. The ``job_id`` in the response can be used to track
    processing status via ``GET /api/v1/jobs/{job_id}``.
    """
    if source_url is None and file is None:
        raise HTTPException(
            status_code=422,
            detail="Either source_url or file must be provided",
        )

    if file is not None:
        if source_type == SourceType.WEB:
            raise HTTPException(
                status_code=422,
                detail="source_type 'web' does not accept file uploads,"
                " provide source_url instead.",
            )
        allowed = ALLOWED_EXTENSIONS.get(source_type, frozenset())
        ext = file_extension(file.filename)
        if ext not in allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"File extension '{ext}' is not allowed "
                    f"for source_type '{source_type}'. "
                    f"Accepted: {sorted(allowed)}"
                ),
            )

    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    actual_filename: str | None = filename
    actual_url: str

    if file is not None:
        if actual_filename is None:
            actual_filename = file.filename
        key = f"{node_id}/{uuid.uuid4()}/{actual_filename or 'upload'}"
        content_type = file.content_type or "application/octet-stream"
        actual_url, uploaded_bytes = await s3.upload_smart(
            stream=upload_file_chunks(file),
            key=key,
            content_type=content_type,
            file_size=file.size,
        )
        logger.info("file_uploaded", key=key, size=uploaded_bytes)
    elif source_url is not None:
        actual_url = source_url

    document_repo = AuthoredDocumentRepository(session)
    document = await document_repo.create(
        node_id=node_id,
        source_type=source_type,
        source_url=actual_url,
        filename=actual_filename,
        material_role=material_role,
        task_type=task_type,
        language=language,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
        material_id=document.id,
        source_type=source_type,
        source_url=actual_url,
    )
    await session.commit()

    logger.info(
        "document_created",
        document_id=str(document.id),
        node_id=str(node_id),
        job_id=str(job.id),
        task_type=task_type,
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id

    warning = check_platform(source_type, actual_url)
    if warning:
        response.warnings.append(warning)

    return response


PRESIGNED_URL_EXPIRY = 900  # 15 minutes


@router.post("/nodes/{node_id}/documents/upload-url")
async def get_upload_url(
    node_id: uuid.UUID,
    body: PresignedUrlRequest,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
) -> PresignedUrlResponse:
    """Generate a presigned URL for direct S3 upload.

    The client should PUT the file content to the returned URL,
    then call ``POST /nodes/{nid}/documents/confirm-upload`` with
    the returned key.
    """
    if body.source_type == SourceType.WEB:
        raise HTTPException(
            status_code=422,
            detail="source_type 'web' does not support file upload.",
        )

    allowed = ALLOWED_EXTENSIONS.get(body.source_type, frozenset())
    ext = file_extension(body.filename)
    if ext not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"File extension '{ext}' is not allowed "
                f"for source_type '{body.source_type}'. "
                f"Accepted: {sorted(allowed)}"
            ),
        )

    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    key = f"tenants/{tenant.tenant_id}/nodes/{node_id}/{uuid.uuid4()}/{body.filename}"

    upload_url = await s3.generate_presigned_url(
        key, body.content_type, expires_in=PRESIGNED_URL_EXPIRY
    )

    logger.info("presigned_url_generated", key=key, node_id=str(node_id))
    return PresignedUrlResponse(
        upload_url=upload_url,
        key=key,
        expires_in=PRESIGNED_URL_EXPIRY,
    )


@router.post("/nodes/{node_id}/documents/confirm-upload", status_code=201)
async def confirm_upload(
    node_id: uuid.UUID,
    body: ConfirmUploadRequest,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
) -> AuthoredDocumentCreateResponse:
    """Confirm a presigned upload and create the AuthoredDocument.

    Verifies the file exists in S3, creates the database entry,
    and enqueues ingestion.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    # Verify key belongs to this tenant and node
    expected_prefix = f"tenants/{tenant.tenant_id}/nodes/{node_id}/"
    if not body.key.startswith(expected_prefix):
        raise HTTPException(
            status_code=403,
            detail="S3 key does not match tenant/node.",
        )

    # Verify file exists in S3
    try:
        await s3.head_object(body.key)
    except Exception:
        raise HTTPException(  # noqa: B904
            status_code=404,
            detail=("File not found in S3. Upload may have failed or expired."),
        )

    actual_filename = body.filename or body.key.rsplit("/", 1)[-1]
    s3_url = f"{s3._endpoint_url}/{s3._bucket}/{body.key}"

    document_repo = AuthoredDocumentRepository(session)
    document = await document_repo.create(
        node_id=node_id,
        source_type=body.source_type,
        source_url=s3_url,
        filename=actual_filename,
        material_role=body.material_role,
        task_type=body.task_type,
        language=body.language,
    )

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=node_id,
        material_id=document.id,
        source_type=body.source_type,
        source_url=s3_url,
    )
    await session.commit()

    logger.info(
        "presigned_upload_confirmed",
        document_id=str(document.id),
        node_id=str(node_id),
        key=body.key,
        job_id=str(job.id),
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id
    return response


@router.get("/nodes/{node_id}/documents")
async def list_documents(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[AuthoredDocumentResponse]:
    """List all authored documents attached to a tree node.

    Returns documents ordered by their position (``order`` field).
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = AuthoredDocumentRepository(session)
    documents = await repo.get_for_node(node_id)
    return [AuthoredDocumentResponse.model_validate(d) for d in documents]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> AuthoredDocumentResponse:
    """Get a single authored document by ID.

    Verified through the node → tenant chain.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )
    return AuthoredDocumentResponse.model_validate(document)


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: uuid.UUID,
    body: AuthoredDocumentUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> AuthoredDocumentResponse:
    """Update document metadata (material_role and/or task_type).

    Only fields explicitly sent in the request body are updated.
    Pass ``task_type: null`` to clear the task flag; omit the field
    to keep the current value.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    fields_set = body.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=422,
            detail="At least one field must be provided.",
        )

    if "material_role" in fields_set and body.material_role is not None:
        document = await document_repo.update_material_role(
            document, material_role=body.material_role
        )

    if "task_type" in fields_set:
        document = await document_repo.update_task_type(
            document, task_type=body.task_type
        )

    await session.commit()

    logger.info(
        "document_updated",
        document_id=str(document_id),
        material_role=body.material_role,
        task_type=body.task_type,
        fields=list(fields_set),
    )
    return AuthoredDocumentResponse.model_validate(document)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
) -> None:
    """KD3 soft-delete the document and any descendants (vision §3 KD3).

    Cascade flow per Phase 1 KD3 adoption — mirrors commit (k)
    ``delete_node`` shape with the cascade rooted at AuthoredDocument
    instead of CourseNode:

    1. Verify the document exists and belongs to the caller's tenant
       (404 otherwise — same shape as the rest of the route).
    2. Extract the S3 key from ``document.source_url`` BEFORE cascade
       fires. Order matters: the cascade engine dispatches
       ``scrub_authored_document`` which sets ``source_url = ''`` —
       collecting the key after cascade would miss it.
    3. Issue ``CascadeDeleteService.soft_delete_with_cascade`` rooted
       at the document. The engine drives the four-phase hook chain
       (cancel → invalidate → scrub → write deleted_at → flush) plus
       per-victim scrub dispatch:

       * ``on_cancel_jobs`` — closure-augmented binding to
         :class:`JobCancellationService.cancel_jobs_for_entities`
         per vision §KD13. Cascade engine passes the document id as
         the victim list, but JCS lookup paths are course_node_id-
         keyed (``Job.course_node_id`` IN, ``Job.input_params @>
         {course_node_id}``, ``Job.tenant_id`` IN); a raw bind would
         silent-no-op. Closure injects ``document.course_node_id``
         so the Job.course_node_id path matches active node-scoped
         ingestion jobs. KD13 cancel semantics:
         ``status='cancelled'`` + ``completed_at = now()``;
         ``deleted_at`` REMAINS NULL — Job is sibling-cancelled, not
         cascade soft-deleted (per PHASE.md §1.2 audit: Job ∉ any
         ``__cascades_soft_delete_to__`` chain).
       * ``on_invalidate_hashes`` — Gap 3 hook bridging
         :class:`ContentHashService.invalidate_subtree` so parent-
         CourseNode ``content_hash`` recompute treats the document as
         already gone. Closes a tangential gap in commit (l) where
         this hook was omitted entirely (cf. ``nodes.py`` +
         ``storage.py`` mirror sites — hotfix-5 ratified per rule #13
         atomicity).
       * Class-level ``__scrub_callable__`` (AuthoredDocument →
         ``scrub_authored_document`` emitting KD3 marker per
         hotfix-4) clears ``filename`` + ``source_url``.
         DocumentSummary + DocumentSegment descendants are no-op in
         Phase 1 — Amendment 16 defers their scrub callables to
         Phase 2.x and pipeline does not write to these tables yet,
         so cascade traversal through them yields zero victims.
    4. Hand off to ``enqueue_s3_cleanup`` — helper persists a
       ``s3_cleanup`` Job row, commits cascade + Job atomically
       (QQ5 boundary), then dispatches the ARQ task post-commit and
       records the resolved ``arq_job_id``. Empty key list (external
       URL or already-scrubbed source_url) short-circuits — no Job
       row, no ARQ enqueue.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    # (2) Capture the S3 key before cascade scrub blanks ``source_url``.
    # ``extract_key`` returns ``None`` for external URLs that don't
    # belong to our bucket — those rows have no S3 cleanup work.
    course_node_id = document.course_node_id
    file_keys: list[str] = []
    s3_key = s3.extract_key(document.source_url)
    if s3_key is not None:
        file_keys.append(s3_key)

    # (3) Cascade soft-delete. Class-level ``__scrub_callable__``
    # dispatch (models-fix-3) clears KD3 fields on the AuthoredDocument
    # in the same flush as the ``deleted_at`` write. Phase 1 cascade
    # map for AuthoredDocument resolves to [DocumentSummary,
    # DocumentSegment] — both empty in Phase 1 per Amendment 16.
    # Hook chain per cascade engine ordering invariant: cancel →
    # invalidate → scrub → write deleted_at → flush. See route
    # docstring step (3) for the closure-augmentation rationale on
    # ``cancel_hook`` (cascade victim ids are document-keyed; JCS
    # lookup paths are course_node_id-keyed).
    cascade_service = CascadeDeleteService(session)
    cascade_map = build_cascade_map(AuthoredDocument)
    content_hash_service = ContentHashService(session)
    job_cancellation_service = JobCancellationService(session)

    async def invalidate_hook(
        ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
    ) -> None:
        await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

    async def cancel_hook(victim_ids: list[uuid.UUID]) -> None:
        augmented = [*victim_ids, course_node_id]
        await job_cancellation_service.cancel_jobs_for_entities(augmented)

    await cascade_service.soft_delete_with_cascade(
        document,
        cascade_map,
        on_cancel_jobs=cancel_hook,
        on_invalidate_hashes=invalidate_hook,
    )

    # (4) Persist the s3_cleanup Job + dispatch ARQ task. Helper owns
    # the QQ5 commit boundary; we pass ``course_node_id`` so tenant
    # scope on the Job row is recoverable via the
    # ``Job.course_node_id → CourseNode.tenant_id`` join even after
    # the cascade scrub clears the document's content fields.
    s3_files_cleaned = len(file_keys)
    if file_keys:
        await enqueue_s3_cleanup(
            session=session,
            arq=arq,
            file_keys=file_keys,
            tenant_id=tenant.tenant_id,
            course_node_id=course_node_id,
        )
    else:
        # No S3 key (external URL or already scrubbed) — still need to
        # commit the cascade soft-delete since the helper would have
        # done it for us otherwise.
        await session.commit()

    logger.info(
        "document_deleted",
        document_id=str(document_id),
        course_node_id=str(course_node_id),
        s3_files_cleaned=s3_files_cleaned,
    )


@router.post("/documents/{document_id}/retry")
async def retry_document(
    document_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    arq: ArqDep,
    force: bool = False,
) -> AuthoredDocumentCreateResponse:
    """Retry ingestion for a document.

    By default only documents in ``error`` state can be retried.
    Pass ``?force=true`` to re-ingest from any state (e.g. to
    reprocess a ``ready`` document after pipeline improvements).
    Returns 409 if the document is not retryable without ``force``.
    Returns 410 Gone (vision §6 QQ6) if the document is soft-deleted —
    retry on a soft-deleted target is permanently unavailable, never
    transient.
    """
    document_repo = AuthoredDocumentRepository(session)
    node_repo = CourseNodeRepository(session)
    document = await _require_document_for_tenant(
        document_repo, node_repo, document_id, tenant.tenant_id
    )

    # QQ6: HTTP 410 Gone on retry of soft-deleted target. Fires BEFORE
    # the state check so a soft-deleted ``error``-state row also yields
    # 410, not 200 (the row's been logically removed; re-ingestion is
    # not a recovery path).
    if document.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Document has been deleted; retry is no longer available.",
        )

    if not force and document.state != "error":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot retry: document is in '{document.state}' state, "
                f"expected 'error'. Use ?force=true to re-ingest."
            ),
        )

    # Clear error and re-enqueue
    document.error_message = None
    await session.flush()

    job = await enqueue_ingestion(
        redis=arq,
        session=session,
        tenant_id=tenant.tenant_id,
        node_id=document.course_node_id,
        material_id=document.id,
        source_type=document.source_type,
        source_url=document.source_url,
    )
    # enqueue_ingestion already flipped the document to PENDING synchronously.
    await session.commit()

    logger.info(
        "document_retry",
        document_id=str(document_id),
        job_id=str(job.id),
    )
    response = AuthoredDocumentCreateResponse.model_validate(document)
    response.job_id = job.id
    return response
