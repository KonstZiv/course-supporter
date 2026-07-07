"""Homework submission API endpoint.

Accepts homework files from external systems, uploads to S3,
creates Student and HomeworkSubmission records, and enqueues
processing into the separate ``homework`` ARQ queue.

Routes
------
- ``POST /homework/submit`` — Submit homework for review (202 Accepted)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    HomeworkSubmitResponse,
    ProjectBaseDescriptorResponse,
    TenantWebhookResponse,
)
from course_supporter.api.url_validation import validate_webhook_url
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.submission_core import (
    create_and_dispatch_submission,
    validate_homework_file,
)
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.student_repository import StudentRepository

if TYPE_CHECKING:
    from course_supporter.storage.orm import Student

logger = structlog.get_logger()

router = APIRouter(tags=["homework"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
CheckDep = Annotated[TenantContext, Depends(require_scope(AuthScope.CHECK))]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]


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
    # --- Validate file (shared pre-upload gate, run first) ---
    validate_homework_file(file)

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

    # --- Upload + create + dispatch via the shared core (mode-1: webhook) ---
    # The student is resolved by get-or-create on external_id, INSIDE the core's
    # S3-cleanup guard (so a resolution failure cleans up the upload, unchanged).
    student_repo = StudentRepository(session)

    async def _resolve_student() -> tuple[Student, bool]:
        return await student_repo.get_or_create(
            tenant_id=tenant.tenant_id,
            external_id=student_external_id,
        )

    result = await create_and_dispatch_submission(
        session=session,
        s3=s3,
        arq=arq,
        tenant_id=tenant.tenant_id,
        resolve_student=_resolve_student,
        course_node_id=course_node_id,
        node_id=node_id,
        authored_document_id=authored_document_id,
        file=file,
        delivery_mode="webhook",
        webhook_url=webhook_url,
        response_language=response_language,
        student_note=student_note,
    )

    if result.duplicate:
        return HomeworkSubmitResponse(
            submission_id=result.submission.id,
            student_id=result.student.id,
            status=result.submission.status,
            job_id=result.job_id,
            duplicate=True,
        )

    return HomeworkSubmitResponse(
        submission_id=result.submission.id,
        student_id=result.student.id,
        status="received",
        job_id=result.job_id,
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


@router.get("/homework/tasks/{authored_document_id}/base")
async def get_task_base(
    authored_document_id: uuid.UUID,
    tenant: CheckDep,
    session: SessionDep,
    s3: S3Dep,
) -> ProjectBaseDescriptorResponse:
    """Base descriptor for a project task (KD18 P2, mode-1 API-key).

    Returns the active base — the latest READY version — with its echo-match
    ``snapshot_hash`` and a presigned GET of the original archive. If a base
    exists but none is READY yet, returns the latest version with its state
    (``pending`` / ``failed``, ``snapshot_hash`` null). 404 if the task carries
    no base at all (not an empty object).
    """
    # Tenant ownership via document → node.
    doc = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    node = await CourseNodeRepository(session).get_by_id(doc.course_node_id)
    if node is None or node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Task not found.")

    pb_repo = ProjectBaseRepository(session)
    base = await pb_repo.get_latest_ready(authored_document_id)
    if base is None:
        base = await pb_repo.get_latest(authored_document_id)
    if base is None:
        raise HTTPException(status_code=404, detail="No base attached to this task.")

    original_url = await s3.generate_presigned_get_url(base.archive_key)
    return ProjectBaseDescriptorResponse(
        base_version_id=base.id,
        version=base.version,
        snapshot_hash=base.snapshot_hash,
        state=base.state,
        original_url=original_url,
    )
