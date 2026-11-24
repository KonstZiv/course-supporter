"""The author's screen of a student's work: submissions and their reviews.

Routes (tenant API key, scope ``PREP``, as the neighbouring ``/students`` routes)
-------------------------------------------------------------------------------
- ``GET /students/{student_id}/submissions`` — the student's submissions, newest
  first, with the courses the student is enrolled in.
- ``GET /students/{student_id}/submissions/{submission_id}`` — one submission:
  everything the student sees of it, plus the verdict on each criterion and the
  pass rule that decided it.
- ``GET /students/{student_id}/submissions/{submission_id}/file`` — a
  short-lived link to download the submitted file.

One boundary for all three: the student and the submission belong to the key's
tenant, neither is soft-deleted, and the submission is this student's. Anything
else is the one 404 of "there is no such thing" — the author never learns that
something exists under another tenant, or under another student.

What the student sees is projected by the very functions of the portal
(``_portal_shared``), so the two screens cannot read a review two ways. What the
author sees beyond it is the verdict rows and the list they were judged against.
The investigation trace — ``safety_result``, ``sanity_result``, the raw
``review_result``, the job, the storage key, every cost figure — never leaves.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Final
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_s3_client, get_session
from course_supporter.api.routes._feedback_shared import to_touch
from course_supporter.api.routes._portal_shared import (
    curated_not_opened,
    curated_presentation,
    curated_recovered_encoding,
    curated_rejection,
    curated_structure,
    curated_verdict,
    delta_receipt,
    material_label,
)
from course_supporter.api.schemas import (
    AuthorCriterionVerdict,
    AuthorPassRule,
    AuthorStudentEnrollment,
    AuthorStudentHeader,
    AuthorStudentSubmissions,
    AuthorSubmissionDetail,
    AuthorSubmissionFileLink,
    AuthorSubmissionListItem,
    AuthorSubmissionStudent,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.billing.held import waiting_for_author
from course_supporter.criteria_kinds import VerdictItemKind, VerdictValue
from course_supporter.feedback_kinds import FeedbackTargetKind
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.criteria_form import Criterion, author_musts
from course_supporter.homework.judged_list import read_judged_list
from course_supporter.models.review_schema import (
    REVIEW_SCHEMA_VERSION,
    review_schema_version,
)
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.models.source import AssignmentType
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.homework_repository import (
    ATTEMPT_STATUSES,
    HomeworkRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkStatus,
    HomeworkSubmission,
    Student,
    SubmissionCriterionVerdict,
)
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = structlog.get_logger()

router = APIRouter(tags=["students"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]

STUDENT_NOT_FOUND: Final = "Student not found."
"""The one text of every way the student of the list cannot be shown."""

SUBMISSION_NOT_FOUND: Final = "Submission not found."
"""The one text of every way a submission cannot be shown: unknown, of another
tenant, of another student, soft-deleted, or of a student who is not the
tenant's any more."""

FILE_LINK_TTL_SEC: Final = 15 * 60
"""How long the download link of a submitted file works: 15 minutes."""

_UNCONFIRMED: Final = "unconfirmed"
_FILENAME_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\;]')
_NON_ASCII = re.compile(r"[^\x20-\x7e]")


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get(
    "/students/{student_id}/submissions",
    response_model=AuthorStudentSubmissions,
)
async def list_student_submissions(
    student_id: Annotated[uuid.UUID, Path(description="The student.")],
    tenant: PrepDep,
    session: SessionDep,
    course_node_id: Annotated[
        uuid.UUID | None,
        Query(description="Only the work in this course (the course's root node)."),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=100, description="Max submissions per page (1-100).")
    ] = 20,
    offset: Annotated[
        int, Query(ge=0, description="Submissions to skip for pagination.")
    ] = 0,
) -> AuthorStudentSubmissions:
    """A student's submissions, newest first, with the student's courses.

    404 (one text) when the student is unknown, of another tenant or
    soft-deleted. Soft-deleted submissions are left out of the page and of the
    total. Courses, tasks and the "waits for the author's units" marks are read
    once for the page, not once per row.
    """
    student = await _require_student(session, tenant, student_id, STUDENT_NOT_FOUND)
    repo = HomeworkRepository(session)
    rows, total = await repo.list_for_author(
        tenant_id=tenant.tenant_id,
        student_id=student.id,
        course_node_id=course_node_id,
        limit=limit,
        offset=offset,
    )
    enrollments = await StudentEnrollmentRepository(session).list_for_student(
        student.id
    )
    courses = await _courses(
        session,
        tenant.tenant_id,
        {e.course_node_id for e in enrollments} | {r.course_node_id for r in rows},
    )
    tasks = await _tasks(session, {r.authored_document_id for r in rows})
    held = await _held_for_author(session, rows, tasks)
    finished = await repo.completion_times(
        r.job_id for r in rows if r.job_id is not None
    )
    credential = await StudentCredentialRepository(session).get_by_student_id(
        student.id
    )
    return AuthorStudentSubmissions(
        student=AuthorStudentHeader(
            student_id=student.id,
            display_name=student.display_name,
            login=credential.login if credential is not None else None,
        ),
        enrollments=[
            AuthorStudentEnrollment(
                course_id=e.course_node_id,
                course_title=courses[e.course_node_id].title,
                enrolled_at=e.created_at,
            )
            for e in enrollments
            if e.course_node_id in courses
            and courses[e.course_node_id].deleted_at is None
        ],
        items=[
            AuthorSubmissionListItem(
                **_common_fields(r, courses, tasks, held, finished),
                passed=_passed(r),
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/students/{student_id}/submissions/{submission_id}",
    response_model=AuthorSubmissionDetail,
)
async def get_student_submission(
    student_id: Annotated[uuid.UUID, Path(description="The student.")],
    submission_id: Annotated[
        uuid.UUID, Path(description="The submission (must be the student's).")
    ],
    tenant: PrepDep,
    session: SessionDep,
) -> AuthorSubmissionDetail:
    """One submission with its review and the verdict on each criterion.

    Everything the student reads of it (verdict, review structure, review text,
    the project's delta, their own answer about the review) plus the course and
    task, when the review was written, the pass rule that decided it and the
    criteria it was judged against. 404 (one text) for every submission the
    boundary of this module does not cover.
    """
    student, submission = await _require_submission(
        session, tenant, student_id, submission_id
    )
    courses = await _courses(session, tenant.tenant_id, {submission.course_node_id})
    tasks = await _tasks(session, {submission.authored_document_id})
    held = await _held_for_author(session, [submission], tasks)
    repo = HomeworkRepository(session)
    finished = await repo.completion_times(
        [submission.job_id] if submission.job_id is not None else []
    )
    structure = curated_structure(submission.review_result, submission_id=submission.id)
    own = await FeedbackRepository(session).get_for_target(
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
        target_kind=FeedbackTargetKind.REVIEW,
        target_id=submission.id,
    )
    criteria, judged = await _criteria(session, submission)
    return AuthorSubmissionDetail(
        **_common_fields(submission, courses, tasks, held, finished),
        student=AuthorSubmissionStudent(
            student_id=student.id, display_name=student.display_name
        ),
        verdict=curated_verdict(
            submission.review_result,
            score=submission.score,
            submission_id=submission.id,
        ),
        structure=structure,
        review_markdown=submission.review_markdown,
        delta=await delta_receipt(session, submission),
        own_feedback=to_touch(own) if own is not None else None,
        pass_rule=_pass_rule(structure, judged),
        criteria=criteria,
    )


@router.get(
    "/students/{student_id}/submissions/{submission_id}/file",
    response_model=AuthorSubmissionFileLink,
)
async def get_student_submission_file(
    student_id: Annotated[uuid.UUID, Path(description="The student.")],
    submission_id: Annotated[
        uuid.UUID, Path(description="The submission (must be the student's).")
    ],
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
) -> AuthorSubmissionFileLink:
    """A link to download the submitted file, good for 15 minutes.

    A download (``Content-Disposition: attachment``) under the student's own
    file name, never rendered in a page. 404 — the one text of this module —
    for a submission the boundary does not cover, and for one that has no file
    of the student's: a test is answered with choices, and what is stored for
    it is the answer sheet, not a file.
    """
    _, submission = await _require_submission(
        session, tenant, student_id, submission_id
    )
    task = await session.get(AuthoredDocument, submission.authored_document_id)
    if task is not None and task.task_type == AssignmentType.TEST.value:
        raise HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)
    key = s3.extract_key(submission.file_url)
    if key is None:
        raise HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)

    issued_at = datetime.now(UTC)
    url = await s3.generate_presigned_get_url(
        key,
        expires_in=FILE_LINK_TTL_SEC,
        content_disposition=_attachment(
            submission.original_filename or key.rsplit("/", 1)[-1]
        ),
    )
    # Who asked for which work, and nothing that opens it: no link, no key.
    logger.info(
        "author_submission_file_link",
        tenant_id=str(tenant.tenant_id),
        student_id=str(student_id),
        submission_id=str(submission.id),
    )
    return AuthorSubmissionFileLink(
        url=url, expires_at=issued_at + timedelta(seconds=FILE_LINK_TTL_SEC)
    )


# ── The boundary ────────────────────────────────────────────────────────────


async def _require_student(
    session: AsyncSession,
    tenant: TenantContext,
    student_id: uuid.UUID,
    not_found: str,
) -> Student:
    """The tenant's live student, or the one 404 of ``not_found``."""
    student = await StudentRepository(session).get_by_id(student_id)
    if (
        student is None
        or student.tenant_id != tenant.tenant_id
        or student.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail=not_found)
    return student


async def _require_submission(
    session: AsyncSession,
    tenant: TenantContext,
    student_id: uuid.UUID,
    submission_id: uuid.UUID,
) -> tuple[Student, HomeworkSubmission]:
    """The live student and their live submission of the tenant, or the one 404."""
    student = await _require_student(session, tenant, student_id, SUBMISSION_NOT_FOUND)
    submission = await HomeworkRepository(session).get_for_author(
        tenant_id=tenant.tenant_id,
        student_id=student.id,
        submission_id=submission_id,
    )
    if submission is None:
        raise HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)
    return student, submission


# ── Batch reads for a page ──────────────────────────────────────────────────


async def _courses(
    session: AsyncSession, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, CourseNode]:
    """The tenant's courses by id, soft-deleted ones too: history keeps its title."""
    if not ids:
        return {}
    result = await session.execute(
        select(CourseNode).where(
            CourseNode.tenant_id == tenant_id, CourseNode.id.in_(ids)
        )
    )
    return {node.id: node for node in result.scalars()}


async def _tasks(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, AuthoredDocument]:
    """The tasks the submissions answer, by id — one read."""
    if not ids:
        return {}
    result = await session.execute(
        select(AuthoredDocument).where(AuthoredDocument.id.in_(ids))
    )
    return {doc.id: doc for doc in result.scalars()}


async def _held_for_author(
    session: AsyncSession,
    submissions: Sequence[HomeworkSubmission],
    tasks: dict[uuid.UUID, AuthoredDocument],
) -> frozenset[uuid.UUID]:
    """Which held submissions wait for their AUTHOR's units, asked once per course.

    The payer of a held review is a function of the tenant, the course and the
    student, and a page is one student's: every held submission of a course
    has the same answer. So the payer is read for one representative per
    course, not per row. The tasks are already in the session, so asking for a
    task by id costs nothing more.
    """
    by_course: dict[uuid.UUID, list[HomeworkSubmission]] = {}
    for submission in submissions:
        task = tasks.get(submission.authored_document_id)
        if task is None or submission.status != HomeworkStatus.AWAITING_FUNDS.value:
            continue
        by_course.setdefault(task.course_root_id, []).append(submission)
    waiting: set[uuid.UUID] = set()
    for group in by_course.values():
        if await waiting_for_author(session, group[:1]):
            waiting.update(s.id for s in group)
    return frozenset(waiting)


# ── Projections ─────────────────────────────────────────────────────────────


def _common_fields(
    submission: HomeworkSubmission,
    courses: dict[uuid.UUID, CourseNode],
    tasks: dict[uuid.UUID, AuthoredDocument],
    held: frozenset[uuid.UUID],
    finished: dict[uuid.UUID, datetime],
) -> dict[str, object]:
    """The fields a list row and a detail share, as ``AuthorSubmissionBase`` has them.

    Returned as keywords for the two responses to extend; a test holds its keys
    to ``AuthorSubmissionBase.model_fields``.
    """
    course = courses.get(submission.course_node_id)
    task = tasks.get(submission.authored_document_id)
    fields: dict[str, object] = {
        "id": submission.id,
        "attempt_no": submission.attempt_no,
        "previous_submission_id": submission.previous_submission_id,
        "created_at": submission.created_at,
        "reviewed_at": _reviewed_at(submission, finished),
        "course_id": submission.course_node_id,
        "course_title": course.title if course is not None else "",
        "task_document_id": submission.authored_document_id,
        "task_title": (
            material_label(
                title=task.title,
                filename=task.filename,
                source_type=task.source_type,
                order=task.order,
            )
            if task is not None
            else ""
        ),
        "task_type": task.task_type if task is not None else None,
        "status": submission.status,
        "presentation": curated_presentation(
            submission, held_for_author=submission.id in held
        ),
        "score": submission.score,
        "rejection": curated_rejection(submission),
        "not_opened": curated_not_opened(submission),
        "recovered_encoding": curated_recovered_encoding(submission),
        "is_repeat_copy": _is_repeat_copy(submission),
        "review_language": _review_language(submission),
        "original_filename": submission.original_filename,
    }
    return fields


def _reviewed_at(
    submission: HomeworkSubmission, finished: dict[uuid.UUID, datetime]
) -> datetime | None:
    """When the review was written: the end of the job that wrote it.

    There is no column for it, and ``updated_at`` moves for other reasons (a
    webhook delivery, a soft delete). The processing job's completion is the
    moment its run ended, review written; the job is the submission's current
    one, so a held review that was continued reads the continuation's end. Only a
    submission that has a review (an attempt) has the time.
    """
    if submission.status not in ATTEMPT_STATUSES or submission.job_id is None:
        return None
    return finished.get(submission.job_id)


def _is_repeat_copy(submission: HomeworkSubmission) -> bool:
    """Whether the review is a copy of the earlier review of the same work."""
    receipt = submission.attempt_receipt
    return isinstance(receipt, dict) and DEDUP_RECEIPT_KEY in receipt


def _review_language(submission: HomeworkSubmission) -> str | None:
    """The review's own language when it has one, else the one requested."""
    review = submission.review_result
    if isinstance(review, dict) and (
        review_schema_version(review) == REVIEW_SCHEMA_VERSION
    ):
        language = review.get("language")
        if isinstance(language, str) and language:
            return language
    return submission.response_language


def _passed(submission: HomeworkSubmission) -> bool | None:
    """The student's own ``passed``, from the portal's own verdict."""
    verdict = curated_verdict(
        submission.review_result, score=submission.score, submission_id=submission.id
    )
    return verdict.passed if verdict is not None else None


async def _criteria(
    session: AsyncSession, submission: HomeworkSubmission
) -> tuple[list[AuthorCriterionVerdict], tuple[Criterion, ...] | None]:
    """The verdict on each item, in the order of the list they were judged by.

    Empty — and no error — when the submission has no verdict rows (an old
    review, a test, a refused work). When the list the rows name cannot be
    read, the answer is empty too, and one warning names the ids only: not the
    work, not the criteria.
    """
    rows = await SubmissionVerdictRepository(session).list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    if not rows:
        return [], None
    try:
        judged = await read_judged_list(session, submission, rows)
    except ValueError:
        logger.warning(
            "author_submission_judged_list_unreadable",
            tenant_id=str(submission.tenant_id),
            submission_id=str(submission.id),
        )
        return [], None
    by_id = {row.item_id: row for row in rows}
    ordered: list[AuthorCriterionVerdict] = []
    for criterion in judged:
        if (row := by_id.pop(criterion.id, None)) is not None:
            ordered.append(
                _criterion_verdict(
                    row,
                    text=criterion.text,
                    must_by_author=criterion.must_by_author is True,
                )
            )
        for point in criterion.mandatory_points:
            if (row := by_id.pop(point.id, None)) is not None:
                ordered.append(_criterion_verdict(row, text=point.text))
    if by_id:
        logger.warning(
            "author_submission_verdicts_outside_list",
            tenant_id=str(submission.tenant_id),
            submission_id=str(submission.id),
            item_ids=sorted(by_id),
        )
    return ordered, judged


def _criterion_verdict(
    row: SubmissionCriterionVerdict, *, text: str, must_by_author: bool = False
) -> AuthorCriterionVerdict:
    """One verdict row with its item's text; an unconfirmed "met" is named so."""
    unconfirmed = row.verdict == VerdictValue.NOT_MET and row.quote_not_found
    return AuthorCriterionVerdict.model_validate(
        {
            "item_id": row.item_id,
            "item_kind": VerdictItemKind(row.item_kind).value,
            "text": text,
            "weight": row.weight,
            "must_by_author": must_by_author,
            "verdict": _UNCONFIRMED if unconfirmed else row.verdict,
            "quote": row.quote,
            "quote_line_start": row.quote_line_start,
            "missing": row.missing,
            "remark_state": row.remark_state,
        }
    )


def _pass_rule(
    structure: ReviewStructureV1 | None, judged: Iterable[Criterion] | None
) -> AuthorPassRule | None:
    """The rule as the review recorded it; only the count of musts is read apart.

    The basis and the threshold are the ones written when the review was
    assembled, never recounted. How many criteria the author made mandatory is
    not among them, so it is read from the list the work was judged against —
    and is 0 for a branch that has none by definition.
    """
    if structure is None or structure.verdict is None or structure.verdict.rule is None:
        return None
    rule = structure.verdict.rule
    required: int | None
    if rule.basis.startswith("no_author_musts"):
        required = 0
    elif judged is not None:
        required = len(author_musts(judged))
    else:
        required = None
    return AuthorPassRule(
        basis=rule.basis, threshold=rule.threshold, required_musts=required
    )


def _attachment(filename: str) -> str:
    """A ``Content-Disposition: attachment`` value for the student's file name.

    An ASCII fallback beside the percent-encoded UTF-8 name (RFC 6266), with the
    characters that could end the quoted value or add a header taken out.
    """
    clean = _FILENAME_UNSAFE.sub("", filename).strip() or "work"
    fallback = _NON_ASCII.sub("_", clean)
    return (
        f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"
    )
