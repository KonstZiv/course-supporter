"""Conflict detection for structure generation requests.

Checks whether a new generation request overlaps with any active
(queued/running) generation job. Two scopes overlap when one is an
ancestor of the other (or they target the same node/course).

Overlap table (from AR-6):
    Active scope  |  New request  |  Result
    Course (all)  |  Node A       |  409 — Node A nested in course
    Node A        |  Node A1      |  409 — A1 nested in A
    Node A        |  Node B       |  202 — independent subtrees
    Node A1       |  Node A2      |  202 — siblings
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import Job, MaterialNode


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """Describes which active job conflicts with the new request."""

    job_id: uuid.UUID
    job_node_id: uuid.UUID | None
    reason: str


async def detect_conflict(
    session: AsyncSession,
    course_id: uuid.UUID,
    target_node_id: uuid.UUID | None,
    active_jobs: list[Job],
) -> ConflictInfo | None:
    """Check if *target_node_id* overlaps with any active generation job.

    Args:
        session: DB session for loading nodes (parent chain walk).
        course_id: Course being generated.
        target_node_id: Target node (None = course-level).
        active_jobs: Active generation jobs for the same course.

    Returns:
        ``ConflictInfo`` for the first conflicting job, or ``None``.
    """
    for job in active_jobs:
        job_node_id = job.node_id
        if _scopes_overlap_fast(target_node_id, job_node_id):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason=_overlap_reason(target_node_id, job_node_id),
            )
        if await _is_ancestor_or_same(
            session, ancestor_node_id=job_node_id, descendant_node_id=target_node_id
        ):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason="target is nested inside active job scope",
            )
        if await _is_ancestor_or_same(
            session, ancestor_node_id=target_node_id, descendant_node_id=job_node_id
        ):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason="active job scope is nested inside target",
            )
    return None


def _scopes_overlap_fast(
    target_node_id: uuid.UUID | None,
    job_node_id: uuid.UUID | None,
) -> bool:
    """Quick overlap check without DB access.

    Two scopes trivially overlap when:
    - Both are course-level (None == None).
    - Both target the exact same node.
    - Either is course-level (None) — course covers everything.
    """
    if target_node_id is None or job_node_id is None:
        return True
    return target_node_id == job_node_id


async def _is_ancestor_or_same(
    session: AsyncSession,
    *,
    ancestor_node_id: uuid.UUID | None,
    descendant_node_id: uuid.UUID | None,
) -> bool:
    """Check if *ancestor_node_id* is an ancestor of *descendant_node_id*.

    Walks the parent chain from descendant up to root.
    Returns ``False`` immediately if either is ``None`` (course-level
    cases are handled by ``_scopes_overlap_fast``).
    """
    if ancestor_node_id is None or descendant_node_id is None:
        return False

    current_id: uuid.UUID | None = descendant_node_id
    visited: set[uuid.UUID] = set()

    while current_id is not None:
        if current_id == ancestor_node_id:
            return True
        if current_id in visited:
            break  # safety: cycle in data
        visited.add(current_id)
        node = await session.get(MaterialNode, current_id)
        if node is None:
            break
        current_id = node.parent_id

    return False


def _overlap_reason(
    target_node_id: uuid.UUID | None,
    job_node_id: uuid.UUID | None,
) -> str:
    """Human-readable conflict reason for fast-path overlaps."""
    if target_node_id is None and job_node_id is None:
        return "both target the entire course"
    if target_node_id == job_node_id:
        return "both target the same node"
    if job_node_id is None:
        return "active job covers entire course"
    # target_node_id is None
    return "new request covers entire course"
