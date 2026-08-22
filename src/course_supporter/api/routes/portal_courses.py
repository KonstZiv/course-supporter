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
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_current_student, get_session
from course_supporter.api.routes._portal_shared import (
    curated_verdict,
    material_label,
    role_visible_to_student,
)
from course_supporter.api.schemas import (
    PortalAttemptResult,
    PortalCourseListItem,
    PortalMaterialItem,
    PortalMaterialTreeNode,
    PortalSubmissionOverlay,
    PortalTaskBase,
)
from course_supporter.auth.context import StudentContext
from course_supporter.models.source import AssignmentType
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    MaterialState,
    ProjectBase,
)
from course_supporter.storage.project_base_repository import ProjectBaseRepository
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


def _base_block(versions: list[ProjectBase]) -> PortalTaskBase | None:
    """Resolve the active base descriptor for one project task (KD18 P5).

    ``latest_ready`` = MAX version among READY, ``latest`` = MAX version —
    mirroring ``ProjectBaseRepository.get_latest_ready`` / ``get_latest`` over a
    pre-fetched version list (one grouped query, no N+1). No versions → ``None``
    (no base attached — a DISTINCT state from a non-ready base, never collapsed).
    A READY version exists → the active base (``state='ready'``, ``snapshot_hash``
    set = the auto-echo source). Else the latest is ``pending`` / ``failed``
    (``snapshot_hash`` null, submit blocked; BE-409 stays authoritative).
    """
    if not versions:
        return None
    ready = [b for b in versions if b.state == "ready"]
    if ready:
        active = max(ready, key=lambda b: b.version)
        return PortalTaskBase(
            version=active.version,
            snapshot_hash=active.snapshot_hash,
            state="ready",
        )
    latest = max(versions, key=lambda b: b.version)
    # DB CHECK constrains state to the three literals; with no READY version the
    # latest is pending | failed. The cast narrows the ORM's plain ``str`` column.
    return PortalTaskBase(
        version=latest.version,
        snapshot_hash=None,
        state=cast(Literal["pending", "failed"], latest.state),
    )


def _project_task_ids(node: CourseNode) -> set[uuid.UUID]:
    """Collect ids of non-deleted PROJECT task documents in a subtree (KD18 P5).

    Drives the single grouped base query: non-project tasks and materials carry
    no base and are never queried.
    """
    ids: set[uuid.UUID] = set()
    for doc in node.documents:
        if doc.deleted_at is None and doc.task_type == AssignmentType.PROJECT.value:
            ids.add(doc.id)
    for child in node.children:
        if child.deleted_at is None:
            ids |= _project_task_ids(child)
    return ids


def _project_document(
    doc: AuthoredDocument,
    overlays: dict[uuid.UUID, list[HomeworkSubmission]],
    bases: dict[uuid.UUID, PortalTaskBase | None],
) -> PortalMaterialItem:
    """Project one document to the curated item, attaching a task overlay.

    A project task also carries ``task_type`` and its active ``base`` descriptor
    (KD18 P5) so the portal renders the base-download + echo affordance without a
    second call. ``base`` is null for a base-less project task (resolved to None
    in the pre-fetched map), a non-project task, or a material.
    """
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
        task_type=doc.task_type,
        base=bases.get(doc.id),
        overlay=overlay,
    )


def _project_node(
    node: CourseNode,
    overlays: dict[uuid.UUID, list[HomeworkSubmission]],
    bases: dict[uuid.UUID, PortalTaskBase | None],
) -> PortalMaterialTreeNode:
    """Recursively project a CourseNode subtree to the curated tree.

    Soft-deleted, non-READY, and methodological documents are dropped
    (publish-gate A + READY-only: a non-READY material has no presentable media
    yet; role allowlist: a methodological document is never shown to a student —
    :func:`role_visible_to_student`). A node left with no visible documents is
    kept as an empty node, not pruned (P4: existence is not content, and pruning
    would also hide legitimately-empty sections under construction). Children
    are already ordered by ``get_subtree``; soft-deleted children (and thus
    their cascade-deleted subtrees) are skipped.
    """
    documents = [
        _project_document(doc, overlays, bases)
        for doc in sorted(
            (
                d
                for d in node.documents
                if d.deleted_at is None
                and d.state == MaterialState.READY
                and role_visible_to_student(d.material_role)
            ),
            key=lambda d: (d.order, d.created_at),
        )
    ]
    children = [
        _project_node(child, overlays, bases)
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
    documents, non-READY documents, and methodological documents (role
    allowlist) are filtered out, each document carries only its
    task-vs-material ``kind`` + (for tasks) a submission overlay, and the
    internal trace never leaks. The overlay is one query per course (no N+1),
    grouped by the task anchor in Python.
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

    # Resolve the active base for every project task in one grouped query
    # (KD18 P5), then group by document in Python — same no-N+1 shape as the
    # overlay above. A project task with no base resolves to None (base-less).
    project_ids = _project_task_ids(subtree[0])
    base_rows = await ProjectBaseRepository(session).list_for_documents(
        sorted(project_ids)
    )
    versions_by_doc: dict[uuid.UUID, list[ProjectBase]] = defaultdict(list)
    for row in base_rows:
        versions_by_doc[row.authored_document_id].append(row)
    bases: dict[uuid.UUID, PortalTaskBase | None] = {
        doc_id: _base_block(versions_by_doc.get(doc_id, [])) for doc_id in project_ids
    }

    # get_subtree returns the roots of the loaded set; for a single course root
    # there is exactly one — the requested root.
    return _project_node(subtree[0], overlays, bases)
