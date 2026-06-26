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
    PortalSubmissionDetail,
    PortalSubmissionListItem,
    PortalSubmitResponse,
)
from course_supporter.auth.context import StudentContext
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
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import HomeworkSubmission, Student
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
    # --- Validate file (shared pre-upload gate, run first) ---
    validate_homework_file(file)

    # --- Resolve + validate the task anchor, derive the node-context ---
    task_doc = await AuthoredDocumentRepository(session).get_by_id(authored_document_id)
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


def _to_detail(submission: HomeworkSubmission) -> PortalSubmissionDetail:
    """Curated detail — adds review_markdown; still no internal trace."""
    return PortalSubmissionDetail(
        id=submission.id,
        status=submission.status,
        score=submission.score,
        verdict=curated_verdict(submission.review_result),
        review_markdown=submission.review_markdown,
        created_at=submission.created_at,
        original_filename=submission.original_filename,
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
    return _to_detail(submission)
