"""The author's overview of a course's homework: by task, and students by tasks (M-10).

Routes (tenant API key, scope ``PREP``, as the neighbouring ``/students`` routes)
-------------------------------------------------------------------------------
- ``GET /nodes/{course_id}/homework/summary`` — for every task of the course
  where a work is expected (``task``, ``short_task``, ``project``, ``test``), in
  the course's order: how many students submitted and how many passed, the mean
  best score, the attempt a student passes on, and the criteria they miss most.
- ``GET /nodes/{course_id}/homework/students`` — the table: one row per student,
  one cell per task (the same tasks, in the same order), by name, paged.

The path takes the form of the course's other author routes
(``/nodes/{course_id}/enrollment-link``): a course is a root node.

The boundary — one for both routes
    The course is the key's tenant's, live, and a root node; anything else is
    the one 404 ("Course not found.") of ``enrollment_links``. A work counts
    when it is the tenant's, on a task of this course (``course_node_id`` is the
    course's root, as in ``student_submissions``), not soft-deleted, and
    **finished with a review** — ``homework_repository.ATTEMPT_STATUSES``, the
    "attempt" of task 13, a repeated review (a copy of the earlier one) included:
    the student did submit it. A student counts when not soft-deleted, and a
    service student (``students.is_test_student``) only with
    ``include_test_students=true``; how many were left out is in the answer, as
    in the billing cost report. The queries are in
    ``storage/course_homework_repository``.

Passed and score
    Whether an attempt is passed is :func:`curated_verdict` of the portal
    (``_portal_shared``) — the function ``student_submissions`` and the portal
    both read a review through — and the score is the submission's ``score``
    column, the number both screens show. Nothing here reads ``review_result``
    on its own.

Last attempt
    "The criteria students miss" are read from each student's LAST attempt
    (what they stand on now), against the list in force. A last attempt that is
    a repeated review has no verdict rows of its own; it stands on the attempt
    it repeats (``dedup_of`` of its receipt). An attempt judged against an
    earlier list, or with no verdicts (a test, an old review), counts for no
    criterion: identifiers are stable within one list only.

What never leaves
    Texts of works, reviews, quotes and remarks. A criterion's own text — the
    list the author wrote or approved — is the one text of the answer, with the
    names of tasks and nodes the author's tree shows.

Cost
    A fixed number of queries per route, however many students and tasks there
    are: the course, its tree, its tasks, the lists in force, the works, the
    verdict tally, the roster. Nothing is read per student.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Annotated, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_session
from course_supporter.api.routes._portal_shared import curated_verdict, material_label
from course_supporter.api.schemas import (
    CourseHomeworkAttemptsToPass,
    CourseHomeworkCell,
    CourseHomeworkCriterion,
    CourseHomeworkStudentRow,
    CourseHomeworkSummary,
    CourseHomeworkTable,
    CourseHomeworkTaskRef,
    CourseHomeworkTaskSummary,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    choose_in_force,
)
from course_supporter.models.source import AssignmentType
from course_supporter.open_courses.service import author_course
from course_supporter.storage.course_homework_repository import (
    IN_FLIGHT_STATUSES,
    CourseHomeworkRepository,
    CriterionTally,
    WorkRow,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.homework_repository import ATTEMPT_STATUSES
from course_supporter.storage.orm import AuthoredDocument, CourseNode

logger = structlog.get_logger()

router = APIRouter(tags=["homework-overview"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]

COURSE_NOT_FOUND: Final = "Course not found."
"""The one text of a course that cannot be shown: unknown, another tenant's, a
child node, or soft-deleted (the text of ``enrollment_links``)."""

HARDEST_LIMIT: Final = 3
"""How many criteria a task lists as the ones most missed."""

MAX_PAGE: Final = 500
"""The most rows one page of the table holds; the default is the same."""

CourseIdPath = Annotated[uuid.UUID, Path(description="The course (its root node).")]
IncludeTestQuery = Annotated[
    bool,
    Query(
        description="Count service (test) students too; by default they are left "
        "out and their number is in the answer."
    ),
]


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get(
    "/nodes/{course_id}/homework/summary",
    response_model=CourseHomeworkSummary,
)
async def get_course_homework_summary(
    course_id: CourseIdPath,
    tenant: PrepDep,
    session: SessionDep,
    include_test_students: IncludeTestQuery = False,
) -> CourseHomeworkSummary:
    """Results by task for the whole course.

    A task nobody has submitted is in the answer with zero counts and null
    shares, so the author sees where nothing has come. 404 (one text) for a
    course the boundary of this module does not cover.
    """
    course, columns = await _course_and_columns(session, tenant, course_id)
    repo = CourseHomeworkRepository(session)
    task_ids = [c.doc.id for c in columns]

    attempts = _group(
        await repo.works(
            tenant_id=tenant.tenant_id,
            course_id=course.id,
            task_ids=task_ids,
            statuses=ATTEMPT_STATUSES,
            include_test_students=include_test_students,
        )
    )
    in_force = await _lists_in_force(repo, columns)
    judged = {
        pair: _judged_attempt(chain).id for pair, chain in attempts.items() if chain
    }
    tallies = await repo.tally_criteria(
        tenant_id=tenant.tenant_id, submission_ids=list(judged.values())
    )
    excluded = (
        0
        if include_test_students
        else await repo.excluded_test_students_with_attempts(
            tenant_id=tenant.tenant_id, course_id=course.id, task_ids=task_ids
        )
    )

    chains_by_task: dict[uuid.UUID, list[list[_Work]]] = defaultdict(list)
    for (task_id, _), chain in attempts.items():
        chains_by_task[task_id].append(chain)
    tallies_by_task: dict[uuid.UUID, list[CriterionTally]] = defaultdict(list)
    for tally in tallies:
        tallies_by_task[tally.task_id].append(tally)

    tasks = [
        _task_summary(
            column,
            chains_by_task.get(column.doc.id, []),
            hardest=_hardest(
                in_force.get(column.doc.id), tallies_by_task.get(column.doc.id, [])
            ),
        )
        for column in columns
    ]
    return CourseHomeworkSummary(
        course_id=course.id,
        course_title=course.title,
        include_test_students=include_test_students,
        excluded_test_students=excluded,
        tasks=tasks,
    )


@router.get(
    "/nodes/{course_id}/homework/students",
    response_model=CourseHomeworkTable,
)
async def get_course_homework_table(
    course_id: CourseIdPath,
    tenant: PrepDep,
    session: SessionDep,
    include_test_students: IncludeTestQuery = False,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE, description="Max students per page (1-500)."),
    ] = MAX_PAGE,
    offset: Annotated[
        int, Query(ge=0, description="Students to skip for pagination.")
    ] = 0,
) -> CourseHomeworkTable:
    """Students by tasks: a row per student, a cell per task.

    Rows are the students enrolled in the course and those who submitted
    without enrolling, by name (nameless last). The columns are the summary's
    tasks, same order. 404 (one text) for a course the boundary does not cover.
    """
    course, columns = await _course_and_columns(session, tenant, course_id)
    repo = CourseHomeworkRepository(session)
    task_ids = [c.doc.id for c in columns]

    roster = await repo.roster_page(
        tenant_id=tenant.tenant_id,
        course_id=course.id,
        task_ids=task_ids,
        include_test_students=include_test_students,
        limit=limit,
        offset=offset,
    )
    total = await repo.roster_total(
        tenant_id=tenant.tenant_id,
        course_id=course.id,
        task_ids=task_ids,
        include_test_students=include_test_students,
    )
    excluded = (
        0
        if include_test_students
        else await repo.roster_excluded_test_students(
            tenant_id=tenant.tenant_id, course_id=course.id, task_ids=task_ids
        )
    )
    works = _group(
        await repo.works(
            tenant_id=tenant.tenant_id,
            course_id=course.id,
            task_ids=task_ids,
            statuses=ATTEMPT_STATUSES | IN_FLIGHT_STATUSES,
            include_test_students=include_test_students,
            student_ids=[r.student_id for r in roster],
        )
    )
    return CourseHomeworkTable(
        course_id=course.id,
        course_title=course.title,
        include_test_students=include_test_students,
        excluded_test_students=excluded,
        tasks=[c.ref() for c in columns],
        students=[
            CourseHomeworkStudentRow(
                student_id=row.student_id,
                display_name=row.display_name,
                login=row.login,
                cells={
                    c.doc.id: _cell(works.get((c.doc.id, row.student_id), []))
                    for c in columns
                },
            )
            for row in roster
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── The course and its columns ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Column:
    """A task of the course with the node it hangs on."""

    doc: AuthoredDocument
    node: CourseNode

    def ref(self) -> CourseHomeworkTaskRef:
        return CourseHomeworkTaskRef.model_validate(
            {
                "task_id": self.doc.id,
                "title": _title(self.doc),
                "task_type": self.doc.task_type,
                "node_id": self.node.id,
                "node_title": self.node.title,
            }
        )


def _title(doc: AuthoredDocument) -> str:
    """The task's name as the author's tree and the portal show it."""
    return material_label(
        title=doc.title,
        filename=doc.filename,
        source_type=doc.source_type,
        order=doc.order,
    )


async def _course_and_columns(
    session: AsyncSession, tenant: TenantContext, course_id: uuid.UUID
) -> tuple[CourseNode, list[_Column]]:
    """The tenant's live course and its tasks in the course's order, or the 404.

    The order is the tree's: a node's own tasks (by ``order``, then creation),
    then its children, each by ``order`` (nulls last), then creation — what the
    portal's course tree shows. A task on a soft-deleted node is not a column.
    """
    course = await author_course(
        session, tenant_id=tenant.tenant_id, course_id=course_id
    )
    if course is None:
        raise HTTPException(status_code=404, detail=COURSE_NOT_FOUND)
    roots = await CourseNodeRepository(session).get_subtree(
        course.id, tenant_id=tenant.tenant_id
    )
    docs = await CourseHomeworkRepository(session).tasks(course.id)
    by_node: dict[uuid.UUID, list[AuthoredDocument]] = defaultdict(list)
    for doc in docs:
        by_node[doc.course_node_id].append(doc)

    columns: list[_Column] = []

    def walk(node: CourseNode) -> None:
        for doc in sorted(
            by_node.get(node.id, []), key=lambda d: (d.order, d.created_at)
        ):
            columns.append(_Column(doc=doc, node=node))
        for child in node.children:
            if child.deleted_at is None:
                walk(child)

    for root in roots:
        walk(root)
    return course, columns


# ── Attempts ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Work:
    """A submission of a student at a task, read for the overview."""

    id: uuid.UUID
    created_at: datetime
    in_flight: bool
    score: int | None
    passed: bool
    attempt_no: int | None
    repeats: int | None
    """The attempt number a repeated review copies, when this one is a copy."""


_Pair = tuple[uuid.UUID, uuid.UUID]
"""(task id, student id)."""


def _group(rows: list[WorkRow]) -> dict[_Pair, list[_Work]]:
    """The works by (task, student), each chain oldest first (the rows come so)."""
    grouped: dict[_Pair, list[_Work]] = defaultdict(list)
    for row in rows:
        grouped[(row.task_id, row.student_id)].append(_work(row))
    return grouped


def _work(row: WorkRow) -> _Work:
    attempt = row.status in ATTEMPT_STATUSES
    return _Work(
        id=row.id,
        created_at=row.created_at,
        in_flight=row.status in IN_FLIGHT_STATUSES,
        score=row.score,
        passed=attempt and _passed(row),
        attempt_no=row.attempt_no,
        repeats=_repeated_attempt(row.attempt_receipt),
    )


def _passed(row: WorkRow) -> bool:
    """Whether the student's own screen calls the attempt passed.

    The portal's verdict (``curated_verdict``), the very call
    ``student_submissions`` makes; a review that has no verdict is not passed.
    """
    verdict = curated_verdict(row.review_result, score=row.score, submission_id=row.id)
    return verdict is not None and verdict.passed


def _repeated_attempt(receipt: dict[str, object] | None) -> int | None:
    """The number of the attempt a repeated review copies, if this is a copy."""
    if not isinstance(receipt, dict):
        return None
    source = receipt.get(DEDUP_RECEIPT_KEY)
    return source if isinstance(source, int) and not isinstance(source, bool) else None


def _attempts(chain: list[_Work]) -> list[_Work]:
    """The attempts of a chain (a review exists), in order."""
    return [w for w in chain if not w.in_flight]


def _judged_attempt(chain: list[_Work]) -> _Work:
    """The attempt the student's verdicts are read from: the last one.

    A last attempt that is a repeated review owns no verdict rows; it stands on
    the attempt it repeats — the latest earlier one of that number.
    """
    last = chain[-1]
    if last.repeats is None:
        return last
    for earlier in reversed(chain[:-1]):
        if earlier.attempt_no == last.repeats:
            return earlier
    return last


# ── The summary ─────────────────────────────────────────────────────────────


def _half_up(numerator: int, denominator: int) -> float:
    """``numerator / denominator`` to two decimals, halves up, in integers."""
    return ((200 * numerator + denominator) // (2 * denominator)) / 100


def _task_summary(
    column: _Column,
    chains: list[list[_Work]],
    *,
    hardest: list[CourseHomeworkCriterion],
) -> CourseHomeworkTaskSummary:
    """The numbers of one task from its students' chains of attempts."""
    submitted = [attempts for chain in chains if (attempts := _attempts(chain))]
    passes: list[int] = []
    bests: list[int] = []
    for attempts in submitted:
        first = next((n for n, w in enumerate(attempts, 1) if w.passed), None)
        if first is not None:
            passes.append(first)
        scores = [w.score for w in attempts if w.score is not None]
        if scores:
            bests.append(max(scores))
    count = len(submitted)
    return CourseHomeworkTaskSummary(
        **column.ref().model_dump(),
        students_submitted=count,
        submissions=sum(len(a) for a in submitted),
        pass_share=_half_up(len(passes), count) if count else None,
        average_score=_half_up(sum(bests), len(bests)) if bests else None,
        attempts_to_pass=(
            CourseHomeworkAttemptsToPass(
                median=float(median(passes)),
                mean=_half_up(sum(passes), len(passes)),
            )
            if passes
            else None
        ),
        hardest_criteria=hardest,
    )


async def _lists_in_force(
    repo: CourseHomeworkRepository, columns: list[_Column]
) -> dict[uuid.UUID, CriteriaInForce]:
    """The criteria list in force of each task that is judged against one.

    Two reads for the whole course, then the service's own choice
    (``choose_in_force``) per task. A test is judged by its answer key, not a
    list. A stored list that cannot be read is no list, and one warning names
    the task.
    """
    judged = [c.doc for c in columns if c.doc.task_type != AssignmentType.TEST.value]
    machine, override = await repo.live_criteria([d.id for d in judged])
    in_force: dict[uuid.UUID, CriteriaInForce] = {}
    for doc in judged:
        try:
            chosen = choose_in_force(
                doc, machine=machine.get(doc.id), override=override.get(doc.id)
            )
        except (ValueError, ValidationError):
            logger.warning("course_homework_list_unreadable", task_id=str(doc.id))
            continue
        if chosen is not None:
            in_force[doc.id] = chosen
    return in_force


def _hardest(
    in_force: CriteriaInForce | None, tallies: list[CriterionTally]
) -> list[CourseHomeworkCriterion]:
    """Up to three criteria of the list in force missed by most students.

    Only the verdicts judged against this very list count; the most missed
    first, ties by the count and then by the list's own order; a criterion
    nobody missed is not "hard".
    """
    if in_force is None:
        return []
    position = {c.id: n for n, c in enumerate(in_force.criteria)}
    by_id: dict[str, Criterion] = {c.id: c for c in in_force.criteria}
    ranked = sorted(
        (
            t
            for t in tallies
            if t.criteria_layer == in_force.layer.value
            and t.criteria_source_id == in_force.source_id
            and t.item_id in by_id
            and t.not_met > 0
        ),
        key=lambda t: (-t.not_met / t.judged, -t.not_met, position[t.item_id]),
    )
    return [
        CourseHomeworkCriterion.model_validate(
            {
                "criterion_id": t.item_id,
                "text": by_id[t.item_id].text,
                "weight": t.weight,
                "not_met_students": t.not_met,
                "judged_students": t.judged,
                "share": _half_up(t.not_met, t.judged),
            }
        )
        for t in ranked[:HARDEST_LIMIT]
    ]


# ── The table ───────────────────────────────────────────────────────────────

CellState = Literal["not_submitted", "in_review", "passed", "not_passed"]


def _cell(chain: list[_Work]) -> CourseHomeworkCell:
    """One student's standing on one task, from their chain of submissions."""
    attempts = _attempts(chain)
    state: CellState
    if any(w.passed for w in attempts):
        state = "passed"
    elif any(w.in_flight for w in chain):
        state = "in_review"
    elif attempts:
        state = "not_passed"
    else:
        state = "not_submitted"
    scores = [w.score for w in attempts if w.score is not None]
    return CourseHomeworkCell(
        state=state,
        best_score=max(scores) if scores else None,
        attempts=len(attempts),
        last_submitted_at=chain[-1].created_at if chain else None,
    )
