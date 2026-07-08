"""Student-portal submission contour + read-path (Phase 6 T2, KD17).

Routes
------
- ``POST /portal/tasks/{authored_document_id}/submissions`` — submit a solution
  from the student's own session (mode-2, ``in_app``).

These live on the native session path (bearer token, ``get_current_student``):
no API key, no scope. The session entry-point reuses the SAME submission core as
mode-1 (``POST /homework/submit``); it differs only in how the student and the
node-context are resolved (from the session + the task anchor, not Form fields)
and in the ``in_app`` delivery marker, which keeps the review off the caller
webhook and on the read-path.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.api.routes._portal_shared import curated_verdict
from course_supporter.api.schemas import (
    PortalBaseDownload,
    PortalDeltaReceipt,
    PortalSubmissionDetail,
    PortalSubmissionListItem,
    PortalSubmitResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.homework.submission_core import (
    MAX_HOMEWORK_SIZE,
    PROJECT_SUBMISSION_MAX_UPLOAD_BYTES,
    create_and_dispatch_submission,
    project_preflight,
    validate_homework_file,
)
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer import compute_delta, manifest_from_jsonb
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import HomeworkSubmission, Student
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]

# A single generic 404 for every "you cannot submit here" case — unknown task,
# non-task document, foreign tenant, or not enrolled — so the portal never leaks
# which tasks exist outside the student's access (rule #12 + P6 two-stage 404).
_TASK_NOT_FOUND = "Task not found."

# Distinct from _TASK_NOT_FOUND: the task IS visible (the student passed the
# access gate) but no READY base exists yet — leaks nothing new, since the
# materials descriptor already exposes the base state (KD18 P5).
_NO_BASE_AVAILABLE = "No base is available for this task yet."


@router.post(
    "/portal/tasks/{authored_document_id}/submissions",
    status_code=202,
    response_model=PortalSubmitResponse,
)
async def submit_portal_homework(
    student: StudentDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
    authored_document_id: Annotated[
        uuid.UUID,
        Path(description="AuthoredDocument (task) the submission answers."),
    ],
    file: Annotated[
        UploadFile,
        File(description="Homework file (code, text, or archive)."),
    ],
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
            "submission (free text).",
        ),
    ] = None,
    base_snapshot_hash: Annotated[
        str | None,
        Form(
            description="KD18 P3: for a project task with a ready base, the "
            "snapshot_hash of the base version this submission was built from "
            "(the portal sends it automatically from the base descriptor). "
            "Required for a project-with-base submission; ignored otherwise.",
        ),
    ] = None,
) -> PortalSubmitResponse:
    """Submit a solution from the student's session (mode-2 in_app).

    The student supplies only the task (``authored_document_id`` in the path) +
    the file; the node-context (root course + specific node) is DERIVED from the
    task anchor (KD15). Gates, in order: file validation → the task is a real,
    ready task in the student's tenant → the student is enrolled in its course
    (Q1) → readiness. Every access failure collapses to a generic 404. The
    submission funnels through the shared core with ``delivery_mode='in_app'``,
    so no webhook fires and it terminates at ``completed`` for the read-path.
    """
    # --- Resolve the task anchor early: its task_type sets the upload cap (a
    # project = 100 MB, Edit I-a) and drives the project preflight below. The
    # resolution is silent here; the 404 validation stays after the file gate. ---
    task_doc = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    is_project = (
        task_doc is not None and task_doc.task_type == AssignmentType.PROJECT.value
    )
    max_upload = (
        PROJECT_SUBMISSION_MAX_UPLOAD_BYTES if is_project else MAX_HOMEWORK_SIZE
    )

    # --- Validate file (shared pre-upload gate, run first) ---
    validate_homework_file(file, max_upload_bytes=max_upload)

    # --- Validate the task anchor (resolved above), derive the node-context ---
    if (
        task_doc is None
        or task_doc.deleted_at is not None
        or task_doc.task_type is None
    ):
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    course_root_id = task_doc.course_root_id
    node_id = task_doc.course_node_id

    # Tenant isolation (defense-in-depth): the task's course must be in the
    # student's tenant. The enrollment gate below also enforces this, but
    # checking the root keeps the derived tenant_id/course_node_id consistent.
    root_node = await CourseNodeRepository(session).get_by_id(course_root_id)
    if root_node is None or root_node.tenant_id != student.tenant_id:
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    # --- Enrollment gate (Q1): submit only into an enrolled course ---
    enrolled = await StudentEnrollmentRepository(session).is_enrolled(
        student.student_id, course_root_id
    )
    if not enrolled:
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    # --- Readiness gate (KD15 §1319): the task must be ready before submit ---
    summary = await DocumentSummaryRepository(session).get_by_authored_document_id(
        authored_document_id
    )
    if summary is None or summary.status != "ready":
        raise HTTPException(
            status_code=409,
            detail="Task is not ready for submissions yet "
            "(its summary has not been generated).",
        )

    # --- Resolve the session student (validated by get_current_student) ---
    student_obj = await session.get(Student, student.student_id)
    if student_obj is None:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    resolved_student: Student = student_obj

    async def _resolve_student() -> tuple[Student, bool]:
        return resolved_student, False

    # --- Project preflight (KD18 P3): archive-only + echo-match → base_id, ALL
    # before any S3 upload so a rejection never orphans a file. Only for a
    # project task; other task types keep the byte-unchanged single-file path. ---
    base_id: uuid.UUID | None = None
    if is_project:
        base_id = await project_preflight(
            session=session,
            task_doc=task_doc,
            file=file,
            base_snapshot_hash=base_snapshot_hash,
        )

    result = await create_and_dispatch_submission(
        session=session,
        s3=s3,
        arq=arq,
        tenant_id=student.tenant_id,
        resolve_student=_resolve_student,
        course_node_id=course_root_id,
        node_id=node_id,
        authored_document_id=authored_document_id,
        file=file,
        delivery_mode="in_app",
        webhook_url=None,
        response_language=response_language,
        student_note=student_note,
        base_id=base_id,
        max_upload_bytes=max_upload,
    )

    if result.duplicate:
        return PortalSubmitResponse(
            submission_id=result.submission.id,
            status=result.submission.status,
            duplicate=True,
        )
    return PortalSubmitResponse(
        submission_id=result.submission.id,
        status="received",
    )


@router.get(
    "/portal/tasks/{authored_document_id}/base",
    response_model=PortalBaseDownload,
)
async def get_portal_task_base(
    student: StudentDep,
    session: SessionDep,
    s3: S3Dep,
    authored_document_id: Annotated[
        uuid.UUID,
        Path(description="The project task whose active base to download."),
    ],
) -> PortalBaseDownload:
    """Presigned GET of a project task's active base ORIGINAL (KD18 P5, KD17).

    The student downloads the AUTHOR's original base archive (never the
    normalized snapshot, KD17) to build their submission. This is the portal
    (bearer-session) counterpart of the mode-1 API-key base route: the same
    active-base = latest-READY resolution and original-archive presign, re-scoped
    to the student's enrollment. Access failures — unknown task, a
    non-project/non-task document, a foreign tenant, or not enrolled — collapse
    to one generic 404 (rule #12). A visible task with no READY base yet returns
    a DISTINCT 404 (the descriptor already exposes the base state, so it leaks
    nothing new).
    """
    task_doc = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    if (
        task_doc is None
        or task_doc.deleted_at is not None
        or task_doc.task_type != AssignmentType.PROJECT.value
    ):
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    # Tenant isolation (defense-in-depth) + enrollment gate (Q1), mirroring the
    # submit route: the base is reachable only inside an enrolled course.
    root_node = await CourseNodeRepository(session).get_by_id(task_doc.course_root_id)
    if root_node is None or root_node.tenant_id != student.tenant_id:
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)
    enrolled = await StudentEnrollmentRepository(session).is_enrolled(
        student.student_id, task_doc.course_root_id
    )
    if not enrolled:
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    # Active base = latest READY (KD18); no READY version → nothing to download.
    base = await ProjectBaseRepository(session).get_latest_ready(authored_document_id)
    if base is None:
        raise HTTPException(status_code=404, detail=_NO_BASE_AVAILABLE)

    original_url = await s3.generate_presigned_get_url(base.archive_key)
    return PortalBaseDownload(original_url=original_url)


def _to_list_item(submission: HomeworkSubmission) -> PortalSubmissionListItem:
    """Curated list item — no review_markdown, no internal trace."""
    return PortalSubmissionListItem(
        id=submission.id,
        status=submission.status,
        score=submission.score,
        verdict=curated_verdict(submission.review_result),
        created_at=submission.created_at,
        original_filename=submission.original_filename,
    )


async def _delta_receipt(
    session: AsyncSession, submission: HomeworkSubmission
) -> PortalDeltaReceipt | None:
    """The I2 delta receipt — counters + staleness, derived on read (KD18 P5).

    Null for a non-project submission (no snapshot manifest → no delta concept),
    a DISTINCT state from an all-zero delta. For a project submission the delta
    is derived (KD-P3-B) from the persisted manifests — the DB stores manifests,
    not counts, and ``compute_delta`` is BE-only. Counters are the sizes of the
    delta path tuples; the hygiene level is intentionally not surfaced.
    """
    raw_sub = submission.snapshot_manifest
    if raw_sub is None:
        # Non-project submission: no delta concept.
        return None
    sub_manifest = manifest_from_jsonb(raw_sub)

    # A project submission with no base attached (or whose base row / manifest is
    # gone) diffs against nothing → everything is new, no staleness.
    all_new = PortalDeltaReceipt(
        changed=0,
        new=len(sub_manifest.included),
        deleted=0,
        base_version=None,
        latest_version=None,
        is_stale=False,
    )
    if submission.base_id is None:
        return all_new

    pb_repo = ProjectBaseRepository(session)
    base = await pb_repo.get_by_id(submission.base_id)
    if base is None or base.manifest is None:
        return all_new

    delta = compute_delta(manifest_from_jsonb(base.manifest), sub_manifest)
    latest_ready = await pb_repo.get_latest_ready(submission.authored_document_id)
    latest_version = latest_ready.version if latest_ready is not None else None
    is_stale = latest_version is not None and base.version < latest_version
    return PortalDeltaReceipt(
        changed=len(delta.changed),
        new=len(delta.new),
        deleted=len(delta.deleted),
        base_version=base.version,
        latest_version=latest_version,
        is_stale=is_stale,
    )


def _to_detail(
    submission: HomeworkSubmission,
    *,
    delta: PortalDeltaReceipt | None = None,
) -> PortalSubmissionDetail:
    """Curated detail — adds review_markdown; still no internal trace.

    ``delta`` (KD18 P5) is the pre-computed I2 receipt for a project submission,
    None for a non-project one. Kept as a defaulted param so the single caller
    stays explicit and no other serialization path is affected.
    """
    return PortalSubmissionDetail(
        id=submission.id,
        status=submission.status,
        score=submission.score,
        verdict=curated_verdict(submission.review_result),
        review_markdown=submission.review_markdown,
        created_at=submission.created_at,
        original_filename=submission.original_filename,
        delta=delta,
    )


@router.get(
    "/portal/tasks/{authored_document_id}/submissions",
    response_model=list[PortalSubmissionListItem],
)
async def list_portal_submissions(
    student: StudentDep,
    session: SessionDep,
    authored_document_id: Annotated[
        uuid.UUID,
        Path(description="AuthoredDocument (task) to list the student's attempts for."),
    ],
) -> list[PortalSubmissionListItem]:
    """List the student's own attempts on a task, newest first (read-path).

    Visibility (Q1): the task is visible if the student OWNS at least one attempt
    on it (own history survives un-enrollment) OR is currently enrolled in its
    course. Otherwise a generic 404 — never leaking which tasks exist outside the
    student's access. An enrolled student with no attempts gets an empty list.
    Soft-deleted attempts are excluded (Q7).
    """
    rows = await HomeworkRepository(session).list_for_student_and_task(
        student.student_id, authored_document_id
    )
    if rows:
        return [_to_list_item(s) for s in rows]

    # No own attempts → visibility requires enrollment in the task's course.
    task_doc = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
    if (
        task_doc is None
        or task_doc.deleted_at is not None
        or task_doc.task_type is None
    ):
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)

    enrolled = await StudentEnrollmentRepository(session).is_enrolled(
        student.student_id, task_doc.course_root_id
    )
    if not enrolled:
        raise HTTPException(status_code=404, detail=_TASK_NOT_FOUND)
    return []


@router.get(
    "/portal/submissions/{submission_id}",
    response_model=PortalSubmissionDetail,
)
async def get_portal_submission(
    student: StudentDep,
    session: SessionDep,
    submission_id: Annotated[
        uuid.UUID,
        Path(description="The submission to read (must be the student's own)."),
    ],
) -> PortalSubmissionDetail:
    """Read one of the student's own submissions — the full curated slice.

    Ownership-only authorization (Q1): a non-owned, unknown, or soft-deleted
    submission collapses to the same generic 404. The internal trace
    (review_result / safety_result / sanity_result) is never serialized — the
    student sees status / score / verdict / review_markdown only.
    """
    submission = await HomeworkRepository(session).get_owned(
        submission_id, student.student_id
    )
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found.")
    delta = await _delta_receipt(session, submission)
    return _to_detail(submission, delta=delta)
