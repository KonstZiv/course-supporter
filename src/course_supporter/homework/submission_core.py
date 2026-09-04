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

from course_supporter.api.upload_validation import (
    PROJECT_ARCHIVE_MAX_UPLOAD_BYTES,
    file_extension,
)
from course_supporter.enqueue import create_homework_job, dispatch_homework
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.security.policies import HOMEWORK_POLICY
from course_supporter.security.stage1 import archive_kind_for_filename
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import upload_file_chunks
from course_supporter.storage.student_repository import StudentRepository

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import (
        AuthoredDocument,
        HomeworkSubmission,
        Student,
    )
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

# 10 MB max file size (single-file KD15 submissions).
MAX_HOMEWORK_SIZE = 10 * 1024 * 1024

# Raw-upload cap for a PROJECT submission (KD18 P3, Edit I-a). Re-exported from
# its shared home (DD-6-Y) so existing callers keep the name they import.
PROJECT_SUBMISSION_MAX_UPLOAD_BYTES = PROJECT_ARCHIVE_MAX_UPLOAD_BYTES

# The door of the submission routes. DERIVED from the Stage 1 policy, not a
# second list: the two disagreed in both directions until now -- eight
# extensions were admitted here and refused by the worker, while ``tgz`` and
# ``pdf`` were refused here and admitted there, so a student met either a
# rejection with no explanation or a 422 for a format the system accepts.
#
# The two sides genuinely differ in SHAPE, not membership: this door reads
# ``file_extension`` ("..py" -> ".py"), Stage 1 reads ``extension_of``
# ("..py" -> "py"). The prefix transform is therefore explicit and one-way, and
# ``TestDoorMatchesPolicy`` locks equality of the two sets AFTER normalisation
# rather than textual identity -- comparing the raw sets would be green while
# meaning nothing.
ALLOWED_HOMEWORK_EXTENSIONS: frozenset[str] = frozenset(
    f".{ext}" for ext in HOMEWORK_POLICY.allowed_extensions
)


def _door_refusal(code: ErrorCategory, details: str) -> dict[str, str]:
    """Body shape for a refusal at the submission door.

    ``{code, details}`` rather than a bare string, matching the project
    preflight the interface already reads (``submissionCodes.ts``). The code is
    an ``ErrorCategory`` value, the same vocabulary the read path returns for a
    refusal decided later — a student meeting the same problem at the door and
    in the worker should not meet two different words for it.

    ``details`` is the fallback the interface shows when it does not recognise
    the code yet; it never carries a library message.
    """
    return {"code": code.value, "details": details}


def _too_large_details(max_upload_bytes: int) -> str:
    return (
        f"The file is larger than {max_upload_bytes // (1024 * 1024)} MB. "
        f"Remove what the work does not need and submit again."
    )


def validate_homework_file(
    file: UploadFile, *, max_upload_bytes: int = MAX_HOMEWORK_SIZE
) -> None:
    """Reject a homework upload by extension allowlist + declared size (422).

    The pre-upload gate, shared by both entry-points and run FIRST (before any
    mode-specific validation) so the failure mode is identical across modes.
    The post-upload size re-check (when ``file.size`` was unknown) lives in
    :func:`create_and_dispatch_submission`.

    ``max_upload_bytes`` is the size cap (KD18 P3, Edit I-a): the default 10 MB
    for single-file KD15 submissions, or ``PROJECT_SUBMISSION_MAX_UPLOAD_BYTES``
    (100 MB) for a project submission — the route resolves it from ``task_type``
    and passes the same value here and to :func:`create_and_dispatch_submission`.
    The extension check is unchanged and task-agnostic, so it still fails first.
    """
    ext = file_extension(file.filename)
    if ext not in ALLOWED_HOMEWORK_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=_door_refusal(
                ErrorCategory.FORBIDDEN_TYPE,
                (
                    f"File extension {ext!r} is not accepted. Send the work as "
                    f"text, a document, code, or an archive."
                ),
            ),
        )

    if file.size is not None and file.size > max_upload_bytes:
        raise HTTPException(
            status_code=422,
            detail=_door_refusal(
                ErrorCategory.SIZE_LIMIT, _too_large_details(max_upload_bytes)
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


async def project_preflight(
    *,
    session: AsyncSession,
    task_doc: AuthoredDocument,
    file: UploadFile,
    base_snapshot_hash: str | None,
) -> uuid.UUID | None:
    """Sync gate for a project submission — resolve the echo-matched base
    version (or reject) BEFORE any S3 upload, so no rejection ever orphans a
    file (KD18 P3).

    Called by both submit routes ONLY when ``task_type == 'project'``; the
    single-file / non-project path never enters here (byte-unchanged). Every
    rejection carries a stable ``code`` (for the P5 FE dictionary) + a 3-part
    human ``details`` (what happened / why it matters / what to do).

    Decision table:

    * file is not an archive -> 422 ``ARCHIVE_ONLY`` (single-file KD15 stays for
      other task types).
    * no base row at all -> ``None`` — no-base: an empty snapshot, delta "all
      new"; the echo is not required.
    * base row(s) exist but none READY -> 409 ``BASE_NOT_READY`` (mirror of the
      summary-readiness 409: the base exists, it is just still being prepared).
    * >= 1 READY version:
        - echo missing / blank -> 422 ``MISSING_BASE_ECHO``.
        - echo present, no match -> 422 ``UNKNOWN_BASE_ECHO``.
        - echo matches any version -> that version's id. An OLDER version is
          accepted (staleness is derived on the read-path, not an error).

    Returns the ``base_id`` to persist at submit-time, or ``None`` for a no-base
    project submission.
    """
    if archive_kind_for_filename(file.filename or "") is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ARCHIVE_ONLY",
                "details": (
                    "This task expects a project archive, but the uploaded file "
                    "is not one. A project submission is diffed against the "
                    "task's base as a whole archive, so a single loose file "
                    "cannot be processed. Re-submit the whole project as a "
                    ".zip / .tar.gz / .tgz / .gz archive."
                ),
            },
        )

    pb_repo = ProjectBaseRepository(session)
    doc_id = task_doc.id
    latest_ready = await pb_repo.get_latest_ready(doc_id)
    if latest_ready is None:
        # No READY version — distinguish "no base at all" from "not ready yet".
        if await pb_repo.get_latest(doc_id) is None:
            return None  # no-base: empty snapshot, delta "all new".
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BASE_NOT_READY",
                "details": (
                    "This task has a base, but it is still being prepared. Your "
                    "submission is diffed against the base's normalized "
                    "snapshot, which does not exist yet. Wait a moment and "
                    "re-submit — the base becomes available once processing "
                    "finishes."
                ),
            },
        )

    echo = (base_snapshot_hash or "").strip()
    if not echo:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MISSING_BASE_ECHO",
                "details": (
                    "This project task has a ready base, but the submission did "
                    "not declare which base version it was built from. The "
                    "declared base_snapshot_hash pins the exact base your "
                    "project started from so the diff is honest. Fetch the base "
                    "descriptor and re-submit including its snapshot_hash."
                ),
            },
        )

    matched = await pb_repo.find_by_snapshot_hash(doc_id, echo)
    if matched is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNKNOWN_BASE_ECHO",
                "details": (
                    "The declared base_snapshot_hash does not match any version "
                    "of this task's base. Only a hash produced by one of this "
                    "task's base versions can be diffed against, so an unknown "
                    "value cannot be reconciled. Re-fetch the current base "
                    "descriptor and re-submit with its snapshot_hash."
                ),
            },
        )
    return matched.id


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
    base_id: uuid.UUID | None = None,
    max_upload_bytes: int = MAX_HOMEWORK_SIZE,
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

    ``base_id`` (KD18 P3) is the echo-matched ProjectBase version resolved by
    :func:`project_preflight` for a project submission (``None`` otherwise); it
    is persisted at create-time. ``max_upload_bytes`` is the post-upload size
    cap (default 10 MB; 100 MB for a project), matched to the pre-upload gate.
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
    if uploaded_bytes > max_upload_bytes:
        await s3.delete_object(key)
        raise HTTPException(
            status_code=422,
            detail=_door_refusal(
                ErrorCategory.SIZE_LIMIT, _too_large_details(max_upload_bytes)
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
            base_id=base_id,
        )

        # Remember the language, but only when the student actually asked for
        # one and only when the answer changed. The column has existed since
        # Phase 6 with no writer, so today every review after the first falls
        # back to the course language even for a student who has said twice
        # which language they read in. One place for both submission routes,
        # because both arrive here with the student already resolved.
        if response_language and student.preferred_language != response_language:
            await StudentRepository(session).set_preferred_language(
                student.id, response_language
            )
            logger.info(
                "student_preferred_language_updated",
                student_id=str(student.id),
                language=response_language,
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
