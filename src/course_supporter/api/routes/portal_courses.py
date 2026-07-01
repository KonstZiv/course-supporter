"""Student-portal materials-listing (Phase 6 T4a, KD17).

Routes
------
- ``GET /portal/courses`` — the student's enrolled courses (id + title only).
- ``GET /portal/courses/{root_id}/materials`` — the course material tree with a
  per-task submission overlay (status / last / best).

Closes the T4a gap: T3 built only ``GET /portal/materials/{id}`` (a URL for a
KNOWN material id); there was no way for a portal session to enumerate courses
or materials. These listing heads add the "courses → course → material tree"
navigation surface.

Like the other portal routes these live on the native session path (bearer
token, ``get_current_student``): no API key, no scope. Visibility follows
publish-gate A (KD17): enrollment IS the publish gate — a student sees exactly
the courses they are enrolled in; the author controls visibility by WHEN they
enroll a student, not by a flag on the node (CourseNode carries no publish
column). Course-scoped access failures collapse to a single generic 404
(rule #12) so the portal never leaks which courses/materials exist outside the
student's access.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.routes._portal_shared import curated_verdict, material_label
from course_supporter.api.schemas import (
    PortalAttemptResult,
    PortalCourseListItem,
    PortalMaterialItem,
    PortalMaterialTreeNode,
    PortalSubmissionOverlay,
)
from course_supporter.auth.context import StudentContext
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    MaterialState,
)
from course_supporter.storage.student_enrollment_repository import (
    StudentEnrollmentRepository,
)

logger = structlog.get_logger()

router = APIRouter(tags=["portal"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StudentDep = Annotated[StudentContext, Depends(get_current_student)]

# One generic 404 for every "you cannot view this course" case — unknown root,
# non-root id, foreign tenant, soft-deleted, or not enrolled — so the portal
# never leaks which courses exist outside the student's access (rule #12).
_COURSE_NOT_FOUND = "Course not found."

# Lifecycle statuses that mean a review has been written.
_REVIEWED_STATUSES = frozenset({"completed", "delivered"})
# Terminal-error statuses (KD15 §1298): safety-violation (rejected) /
# sanity-mismatch (mismatch) / processing-failure (failed). Surfaced as one
# coarse "error" bucket in the overlay; the precise terminal lives in the
# read-path detail.
_ERROR_STATUSES = frozenset({"rejected", "mismatch", "failed"})


@router.get("/portal/courses", response_model=list[PortalCourseListItem])
async def list_portal_courses(
    student: StudentDep,
    session: SessionDep,
) -> list[PortalCourseListItem]:
    """List the student's enrolled courses — bare (id + title only).

    The landing screen is deliberately cheap: no counts, no peek into the tree
    (that is the per-course materials endpoint). Enrollment IS the visibility
    gate (publish-gate A): the list is exactly the student's active enrollments;
    soft-deleted course roots are filtered out. An un-enrolled student gets an
    empty list — never an error (there is nothing whose existence to leak).
    """
    courses = await StudentEnrollmentRepository(session).list_enrolled_courses(
        student.student_id
    )
    return [
        PortalCourseListItem(id=course.id, title=course.title) for course in courses
    ]


def _overlay_status(status: str) -> Literal["pending", "reviewed", "error"]:
    """Map a lifecycle status to the coarse overlay status.

    Three buckets: ``reviewed`` = a review is written (``completed`` /
    ``delivered``); ``error`` = a terminal error (``rejected`` / ``mismatch`` /
    ``failed``); ``pending`` = everything else, i.e. in-flight (``received`` /
    ``safety_ok`` / ``sanity_ok`` / ``reviewing``). The overlay deliberately
    carries only this coarse ``error`` bucket — the precise terminal (and any
    human-readable reason) is the read-path detail's concern.
    """
    if status in _REVIEWED_STATUSES:
        return "reviewed"
    if status in _ERROR_STATUSES:
        return "error"
    return "pending"


def _build_overlay(attempts: list[HomeworkSubmission]) -> PortalSubmissionOverlay:
    """Overlay for ONE task from its attempts (newest-first).

    ``submission_status`` is by the latest attempt; ``last`` is that attempt's
    curated result; ``best`` is the highest-scored REVIEWED attempt with a
    non-null score (pending / null-score attempts never compete) — absent when
    there is no usable score yet.
    """
    if not attempts:
        return PortalSubmissionOverlay(submission_status="none")
    latest = attempts[0]
    last = PortalAttemptResult(
        score=latest.score,
        verdict=curated_verdict(latest.review_result),
    )
    scored = [
        s
        for s in attempts
        if _overlay_status(s.status) == "reviewed" and s.score is not None
    ]
    best: PortalAttemptResult | None = None
    if scored:
        top = max(scored, key=lambda s: s.score or 0)
        best = PortalAttemptResult(
            score=top.score,
            verdict=curated_verdict(top.review_result),
        )
    return PortalSubmissionOverlay(
        submission_status=_overlay_status(latest.status),
        last=last,
        best=best,
    )


def _project_document(
    doc: AuthoredDocument,
    overlays: dict[uuid.UUID, list[HomeworkSubmission]],
) -> PortalMaterialItem:
    """Project one document to the curated item, attaching a task overlay."""
    is_task = doc.task_type is not None
    overlay = _build_overlay(overlays.get(doc.id, [])) if is_task else None
    return PortalMaterialItem(
        id=doc.id,
        kind="task" if is_task else "material",
        label=material_label(
            filename=doc.filename,
            source_type=doc.source_type,
            order=doc.order,
        ),
        source_type=doc.source_type,
        order=doc.order,
        overlay=overlay,
    )


def _project_node(
    node: CourseNode,
    overlays: dict[uuid.UUID, list[HomeworkSubmission]],
) -> PortalMaterialTreeNode:
    """Recursively project a CourseNode subtree to the curated tree.

    Soft-deleted documents and non-READY documents are dropped (publish-gate A
    + READY-only: a non-READY material has no presentable media yet). Children
    are already ordered by ``get_subtree``; soft-deleted children (and thus
    their cascade-deleted subtrees) are skipped.
    """
    documents = [
        _project_document(doc, overlays)
        for doc in sorted(
            (
                d
                for d in node.documents
                if d.deleted_at is None and d.state == MaterialState.READY
            ),
            key=lambda d: (d.order, d.created_at),
        )
    ]
    children = [
        _project_node(child, overlays)
        for child in node.children
        if child.deleted_at is None
    ]
    return PortalMaterialTreeNode(
        id=node.id,
        title=node.title,
        order=node.order if node.order is not None else 0,
        documents=documents,
        children=children,
    )


@router.get(
    "/portal/courses/{root_id}/materials",
    response_model=PortalMaterialTreeNode,
)
async def get_portal_course_materials(
    student: StudentDep,
    session: SessionDep,
    root_id: Annotated[
        uuid.UUID,
        Path(description="The course (root CourseNode) to render the tree for."),
    ],
) -> PortalMaterialTreeNode:
    """Return the course material tree with a per-task submission overlay.

    Enrollment-gated (publish-gate A); any access failure — unknown root, a
    non-root id, a foreign tenant, soft-deleted, or not enrolled — collapses to
    one generic 404 (rule #12). The tree is curated: soft-deleted nodes /
    documents and non-READY documents are filtered out, each document carries
    only its task-vs-material ``kind`` + (for tasks) a submission overlay, and
    the internal trace never leaks. The overlay is one query per course
    (no N+1), grouped by the task anchor in Python.
    """
    node_repo = CourseNodeRepository(session)
    root = await node_repo.get_by_id(root_id)
    if (
        root is None
        or root.deleted_at is not None
        or root.parent_id is not None
        or root.tenant_id != student.tenant_id
    ):
        raise HTTPException(status_code=404, detail=_COURSE_NOT_FOUND)

    enrolled = await StudentEnrollmentRepository(session).is_enrolled(
        student.student_id, root_id
    )
    if not enrolled:
        raise HTTPException(status_code=404, detail=_COURSE_NOT_FOUND)

    subtree = await node_repo.get_subtree(
        root_id, include_documents=True, tenant_id=student.tenant_id
    )
    if not subtree:
        raise HTTPException(status_code=404, detail=_COURSE_NOT_FOUND)

    attempts = await HomeworkRepository(session).list_active_submissions_in_course(
        student.student_id, root_id
    )
    overlays: dict[uuid.UUID, list[HomeworkSubmission]] = defaultdict(list)
    for attempt in attempts:
        overlays[attempt.authored_document_id].append(attempt)

    # get_subtree returns the roots of the loaded set; for a single course root
    # there is exactly one — the requested root.
    return _project_node(subtree[0], overlays)
