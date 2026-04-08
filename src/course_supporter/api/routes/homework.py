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
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import HomeworkSubmitResponse
from course_supporter.api.upload_validation import file_extension
from course_supporter.api.url_validation import validate_webhook_url
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.enqueue import enqueue_homework
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.s3 import S3Client, upload_file_chunks
from course_supporter.storage.student_repository import StudentRepository

logger = structlog.get_logger()

router = APIRouter(tags=["homework"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
CheckDep = Annotated[TenantContext, Depends(require_scope(AuthScope.CHECK))]
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
        Form(description="Root MaterialNode UUID representing the course."),
    ],
    node_id: Annotated[
        uuid.UUID,
        Form(description="Specific course node the submission targets."),
    ],
    file: Annotated[
        UploadFile,
        File(description="Homework file (code, text, or archive)."),
    ],
    task_id: Annotated[
        uuid.UUID | None,
        Form(description="Optional task ID hint for matching."),
    ] = None,
    webhook_url: Annotated[
        str | None,
        Form(
            description="Per-submission webhook URL override "
            "(falls back to tenant default).",
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
    node_repo = MaterialNodeRepository(session)

    course_node = await node_repo.get_by_id(course_node_id)
    if course_node is None or course_node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Course node not found.")

    if course_node.parent_materialnode_id is not None:
        raise HTTPException(
            status_code=422,
            detail="course_node_id must be a root node (course level).",
        )

    target_node = await node_repo.get_by_id(node_id)
    if target_node is None or target_node.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="Node not found.")

    # --- Upload file to S3 ---
    submission_id = uuid.uuid4()
    filename = file.filename or "upload"
    key = f"homework/{tenant.tenant_id}/{submission_id}/{filename}"
    content_type = file.content_type or "application/octet-stream"

    s3_url, uploaded_bytes = await s3.upload_smart(
        stream=upload_file_chunks(file),
        key=key,
        content_type=content_type,
        file_size=file.size,
    )

    # Check actual uploaded size (in case file.size was None)
    if uploaded_bytes > MAX_HOMEWORK_SIZE:
        # Clean up the oversized upload
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

        # Create submission
        hw_repo = HomeworkRepository(session)
        submission = await hw_repo.create(
            tenant_id=tenant.tenant_id,
            student_id=student.id,
            course_node_id=course_node_id,
            node_id=node_id,
            file_url=s3_url,
            file_type=content_type,
            original_filename=file.filename,
            task_hint_id=task_id,
            webhook_url=webhook_url,
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
    )

    return HomeworkSubmitResponse(
        submission_id=submission.id,
        student_id=student.id,
        status="received",
        job_id=job.id,
    )
