"""Shared submission-creation core for both homework entry-points (Phase 6 T2).

Both ways a homework submission is created funnel through here:

- mode-1 — ``POST /homework/submit`` (caller-owned student, API key, review
  delivered by webhook);
- mode-2 — the portal session entry-point (``in_app``, KD17), where the student
  submits from their own session and reads the review via the read-path.

The shared core owns everything from the S3 upload through the ARQ dispatch:
the durability ordering (DD-3.2.6-A — commit the durable rows BEFORE dispatch)
and the S3-cleanup guard. Each caller does its OWN mode-specific validation,
resolves the node-context (mode-1 from Form fields; mode-2 derived from the
``authored_document_id`` anchor), and resolves the student BEFORE calling here.

``create_and_dispatch_submission`` is the byte-identical extraction of mode-1's
former inline body: ``resolve_student`` is invoked INSIDE the S3-cleanup guard,
so a student-resolution failure cleans up the uploaded file exactly as before.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from fastapi import HTTPException, UploadFile

from course_supporter.api.upload_validation import file_extension
from course_supporter.enqueue import create_homework_job, dispatch_homework
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.s3 import upload_file_chunks

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import HomeworkSubmission, Student
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

# 10 MB max file size.
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


def validate_homework_file(file: UploadFile) -> None:
    """Reject a homework upload by extension allowlist + declared size (422).

    The pre-upload gate, shared by both entry-points and run FIRST (before any
    mode-specific validation) so the failure mode is identical across modes.
    The post-upload size re-check (when ``file.size`` was unknown) lives in
    :func:`create_and_dispatch_submission`.
    """
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


@dataclass(frozen=True)
class SubmissionDispatch:
    """Outcome of the shared submission-creation core.

    On a duplicate (same student + task + file hash already terminal), no new
    row or Job is created: ``duplicate`` is True, ``submission`` is the existing
    row, and ``job_id`` is its original job. Otherwise ``submission`` is the new
    row and ``job_id`` is the freshly dispatched Job.
    """

    submission: HomeworkSubmission
    student: Student
    student_created: bool
    duplicate: bool
    job_id: uuid.UUID | None


async def create_and_dispatch_submission(
    *,
    session: AsyncSession,
    s3: S3Client,
    arq: ArqRedis,
    tenant_id: uuid.UUID,
    resolve_student: Callable[[], Awaitable[tuple[Student, bool]]],
    course_node_id: uuid.UUID,
    node_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    file: UploadFile,
    delivery_mode: str,
    webhook_url: str | None = None,
    response_language: str | None = None,
    student_note: str | None = None,
) -> SubmissionDispatch:
    """Upload to S3, create the submission + Job, then dispatch to ARQ.

    The caller has already validated the file (:func:`validate_homework_file`),
    resolved the node-context, and prepared ``resolve_student``. This function:
    streams the file to S3 (computing its SHA-256), re-checks the uploaded size,
    then — inside an S3-cleanup guard — resolves the student, dedupes, creates
    the submission + durable Job, commits, and dispatches AFTER the commit
    (DD-3.2.6-A). A failure before the commit deletes the uploaded file; a
    dispatch failure leaves the committed submission re-dispatchable.

    ``resolve_student`` runs inside the guard so a resolution failure cleans up
    the upload — preserving mode-1's exact behaviour after the extraction.
    """
    submission_id = uuid.uuid4()
    filename = file.filename or "upload"
    key = f"homework/{tenant_id}/{submission_id}/{filename}"
    content_type = file.content_type or "application/octet-stream"

    stream, hasher = await _hashing_upload(file)
    s3_url, uploaded_bytes = await s3.upload_smart(
        stream=stream,
        key=key,
        content_type=content_type,
        file_size=file.size,
    )
    file_hash = hasher.hexdigest()

    # Check actual uploaded size (in case file.size was None).
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

    try:
        student, student_created = await resolve_student()
        if student_created:
            logger.info("student_created", student_id=str(student.id))

        hw_repo = HomeworkRepository(session)

        # Dedup: identical file already reviewed for this student + task.
        existing = await hw_repo.find_duplicate(
            student_id=student.id,
            authored_document_id=authored_document_id,
            file_hash=file_hash,
        )
        if existing is not None:
            await s3.delete_object(key)
            logger.warning(
                "homework_duplicate_detected",
                student_id=str(student.id),
                authored_document_id=str(authored_document_id),
                file_hash=file_hash,
                existing_submission_id=str(existing.id),
            )
            return SubmissionDispatch(
                submission=existing,
                student=student,
                student_created=student_created,
                duplicate=True,
                job_id=existing.job_id,
            )

        submission = await hw_repo.create(
            tenant_id=tenant_id,
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
            delivery_mode=delivery_mode,
        )

        # Create the durable Job + submission↔job link (DB only — no dispatch
        # yet), then commit everything staged (student + submission + job).
        job = await create_homework_job(
            session=session,
            tenant_id=tenant_id,
            submission_id=submission.id,
        )
        await session.commit()
    except Exception:
        # Clean up the orphaned S3 file if DB operations fail (BEFORE commit).
        await s3.delete_object(key)
        raise

    # Dispatch to ARQ AFTER the commit (DD-3.2.6-A): the worker can only pick up
    # the job once its rows are durable, closing the "Job not found" race. This
    # is deliberately outside the S3-cleanup guard — a dispatch failure leaves a
    # durable, re-dispatchable submission with its file intact.
    await dispatch_homework(
        redis=arq,
        session=session,
        job_id=job.id,
        submission_id=submission.id,
    )

    logger.info(
        "homework_submitted",
        submission_id=str(submission.id),
        student_id=str(student.id),
        job_id=str(job.id),
        file_hash=file_hash,
        delivery_mode=delivery_mode,
    )

    return SubmissionDispatch(
        submission=submission,
        student=student,
        student_created=student_created,
        duplicate=False,
        job_id=job.id,
    )
