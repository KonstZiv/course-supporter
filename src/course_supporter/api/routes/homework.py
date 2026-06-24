"""Homework submission API endpoint.

Accepts homework files from external systems, uploads to S3,
creates Student and HomeworkSubmission records, and enqueues
processing into the separate ``homework`` ARQ queue.

Routes
------
- ``POST /homework/submit`` — Submit homework for review (202 Accepted)
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import HomeworkSubmitResponse, TenantWebhookResponse
from course_supporter.api.upload_validation import file_extension
from course_supporter.api.url_validation import validate_webhook_url
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_homework
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.s3 import S3Client, upload_file_chunks
from course_supporter.storage.student_repository import StudentRepository

logger = structlog.get_logger()

router = APIRouter(tags=["homework"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
CheckDep = Annotated[TenantContext, Depends(require_scope(AuthScope.CHECK))]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

# 10 MB max file size
MAX_HOMEWORK_SIZE = 10 * 1024 * 1024

ALLOWED_HOMEWORK_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".java",
        ".c",
        ".cpp",
        ".cs",
        ".sql",
        ".md",
        ".txt",
        ".html",
        ".ipynb",
        ".zip",
        ".gz",
    }
)


async def _hashing_upload(
    file: UploadFile,
) -> tuple[AsyncIterator[bytes], hashlib._Hash]:
    """Wrap upload_file_chunks to compute SHA-256 while streaming."""
    hasher = hashlib.sha256()

    async def _stream() -> AsyncIterator[bytes]:
        async for chunk in upload_file_chunks(file):
            hasher.update(chunk)
            yield chunk

    return _stream(), hasher


@router.post("/homework/submit", status_code=202)
async def submit_homework(
    tenant: CheckDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    student_external_id: Annotated[
        str,
        Form(description="Student identifier from the caller's system."),
    ],
    course_node_id: Annotated[
        uuid.UUID,
        Form(description="Root CourseNode UUID representing the course."),
    ],
    node_id: Annotated[
        uuid.UUID,
        Form(description="Specific course node the submission targets."),
    ],
    file: Annotated[
        UploadFile,
        File(description="Homework file (code, text, or archive)."),
    ],
    authored_document_id: Annotated[
        uuid.UUID,
        Form(
            description="AuthoredDocument (task) the submission answers — the "
            "single submission↔task anchor (KD15). Must be a task "
            "(task_type set), belong to this course, and not be deleted.",
        ),
    ],
    webhook_url: Annotated[
        str | None,
        Form(
            description="Per-submission webhook URL override "
            "(falls back to tenant default).",
        ),
    ] = None,
    response_language: Annotated[
        str | None,
        Form(
            description="ISO 639-1 language for the review response "
            "(e.g. uk, en). Auto-detected if omitted.",
        ),
    ] = None,
    student_note: Annotated[
        str | None,
        Form(
            description="D7-local: the student's comment or question for this "
            "submission (free text). Steers the Mentor review of this one "
            "attempt; distinct from the student's standing mentor_preferences "
            "(D7-global), which is not a submission field.",
        ),
    ] = None,
) -> HomeworkSubmitResponse:
    """Submit homework for review.

    Accepts a file upload with metadata, stores it in S3,
    creates a Student (if new) and HomeworkSubmission record,
    and enqueues processing into the homework queue.

    Returns 202 Accepted with submission and job IDs for tracking.
    """
    # --- Validate file ---
    ext = file_extension(file.filename)
    if ext not in ALLOWED_HOMEWORK_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"File extension '{ext}' is not allowed. "
                f"Accepted: {sorted(ALLOWED_HOMEWORK_EXTENSIONS)}"
            ),
        )

    if file.size is not None and file.size > MAX_HOMEWORK_SIZE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"File too large ({file.size} bytes). "
                f"Maximum: {MAX_HOMEWORK_SIZE} bytes (10 MB)."
            ),
        )

    # --- Validate webhook URL (SSRF protection) ---
    if webhook_url is not None:
        webhook_url = await validate_webhook_url(webhook_url)

    # --- Verify nodes belong to tenant ---
    node_repo = CourseNodeRepository(session)

    course_node = await node_repo.get_by_id(course_node_id)
    if course_node is None or course_node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Course node not found.")

    if course_node.parent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="course_node_id must be a root node (course level).",
        )

    target_node = await node_repo.get_by_id(node_id)
    if target_node is None or target_node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Node not found.")

    # --- Verify the anchor: authored_document_id is a real task in this course
    # (KD15 referential validation — structural, not the T7 sanity classifier). ---
    doc_repo = AuthoredDocumentRepository(session)
    task_doc = await doc_repo.get_by_id(authored_document_id)
    if (
        task_doc is None
        or task_doc.deleted_at is not None
        or task_doc.course_root_id != course_node_id
    ):
        # Unknown, soft-deleted, or belongs to another course/tenant —
        # do not leak existence across the tenant boundary.
        raise HTTPException(status_code=404, detail="Task not found.")
    if task_doc.task_type is None:
        raise HTTPException(
            status_code=422,
            detail="authored_document_id must reference a task "
            "(an AuthoredDocument with task_type set).",
        )

    # --- Readiness gate (KD15 §1319): the task must be ready (its
    # DocumentSummary formed) before it accepts submissions. ---
    # A submission against an un-ingested task would reach the review graph with
    # empty grounding (degraded criteria + context), producing a low-quality
    # review; reject early and cleanly instead. 409 Conflict, not 404 — the task
    # exists, it is just not ready yet. The graph's degrade-tolerance stays as
    # defense-in-depth, not the primary gate.
    summary_repo = DocumentSummaryRepository(session)
    summary = await summary_repo.get_by_authored_document_id(authored_document_id)
    if summary is None or summary.status != "ready":
        raise HTTPException(
            status_code=409,
            detail="Task is not ready for submissions yet "
            "(its summary has not been generated).",
        )

    # --- Upload file to S3 (streaming SHA-256) ---
    submission_id = uuid.uuid4()
    filename = file.filename or "upload"
    key = f"homework/{tenant.tenant_id}/{submission_id}/{filename}"
    content_type = file.content_type or "application/octet-stream"

    stream, hasher = await _hashing_upload(file)
    s3_url, uploaded_bytes = await s3.upload_smart(
        stream=stream,
        key=key,
        content_type=content_type,
        file_size=file.size,
    )
    file_hash = hasher.hexdigest()

    # Check actual uploaded size (in case file.size was None)
    if uploaded_bytes > MAX_HOMEWORK_SIZE:
        await s3.delete_object(key)
        raise HTTPException(
            status_code=422,
            detail=(
                f"File too large ({uploaded_bytes} bytes). "
                f"Maximum: {MAX_HOMEWORK_SIZE} bytes (10 MB)."
            ),
        )

    logger.info(
        "homework_file_uploaded",
        key=key,
        size=uploaded_bytes,
        content_type=content_type,
        file_hash=file_hash,
    )

    # --- Create DB records (clean up S3 on failure) ---
    try:
        # Get or create student
        student_repo = StudentRepository(session)
        student, created = await student_repo.get_or_create(
            tenant_id=tenant.tenant_id,
            external_id=student_external_id,
        )
        if created:
            logger.info(
                "student_created",
                student_id=str(student.id),
                external_id=student_external_id,
            )

        # --- Dedup: check for identical file already reviewed ---
        hw_repo = HomeworkRepository(session)
        existing = await hw_repo.find_duplicate(
            student_id=student.id,
            authored_document_id=authored_document_id,
            file_hash=file_hash,
        )
        if existing is not None:
            # Identical file already processed — return cached result
            await s3.delete_object(key)
            logger.warning(
                "homework_duplicate_detected",
                student_id=str(student.id),
                node_id=str(node_id),
                file_hash=file_hash,
                existing_submission_id=str(existing.id),
            )
            return HomeworkSubmitResponse(
                submission_id=existing.id,
                student_id=student.id,
                status=existing.status,
                job_id=existing.job_id,
                duplicate=True,
            )

        # Create submission
        submission = await hw_repo.create(
            tenant_id=tenant.tenant_id,
            student_id=student.id,
            course_node_id=course_node_id,
            node_id=node_id,
            authored_document_id=authored_document_id,
            file_url=s3_url,
            file_type=content_type,
            original_filename=file.filename,
            webhook_url=webhook_url,
            file_hash=file_hash,
            response_language=response_language,
            student_note=student_note,
        )

        # Enqueue processing
        job = await enqueue_homework(
            redis=arq,
            session=session,
            tenant_id=tenant.tenant_id,
            submission_id=submission.id,
        )

        await hw_repo.set_job_id(submission.id, job.id)
        await session.commit()
    except Exception:
        # Clean up orphaned S3 file if DB operations fail
        await s3.delete_object(key)
        raise

    logger.info(
        "homework_submitted",
        submission_id=str(submission.id),
        student_id=str(student.id),
        node_id=str(node_id),
        job_id=str(job.id),
        file_hash=file_hash,
    )

    return HomeworkSubmitResponse(
        submission_id=submission.id,
        student_id=student.id,
        status="received",
        job_id=job.id,
    )


@router.patch(
    "/tenant/webhook",
    response_model=TenantWebhookResponse,
    summary="Set or clear the tenant default webhook URL",
)
async def update_tenant_webhook(
    tenant: PrepDep,
    session: SessionDep,
    webhook_url: Annotated[
        str | None,
        Form(description="Default webhook URL, or empty/null to clear."),
    ] = None,
) -> TenantWebhookResponse:
    """Update the default webhook URL for the authenticated tenant.

    This URL is used as fallback when a homework submission does not
    specify its own ``webhook_url``.  Send empty string or omit the
    field to clear the default.
    """
    from course_supporter.storage.orm import Tenant as TenantModel

    # Validate non-empty URL against SSRF
    if webhook_url:
        webhook_url = await validate_webhook_url(webhook_url)
    else:
        webhook_url = None

    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(TenantModel)
        .where(TenantModel.id == tenant.tenant_id)
        .values(webhook_url=webhook_url)
    )
    await session.commit()

    logger.info(
        "tenant_webhook_updated",
        tenant_id=str(tenant.tenant_id),
        webhook_url=webhook_url,
    )

    return TenantWebhookResponse(webhook_url=webhook_url)
