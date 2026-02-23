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

from sqlalchemy import select
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

    Loads all nodes for the course in a single query, builds an
    in-memory parent map, then checks ancestor relationships without
    additional DB round-trips.

    Args:
        session: DB session for loading course nodes.
        course_id: Course being generated.
        target_node_id: Target node (None = course-level).
        active_jobs: Active generation jobs for the same course.

    Returns:
        ``ConflictInfo`` for the first conflicting job, or ``None``.
    """
    parent_map = await _load_parent_map(session, course_id)

    for job in active_jobs:
        job_node_id = job.node_id
        if _scopes_overlap_fast(target_node_id, job_node_id):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason=_overlap_reason(target_node_id, job_node_id),
            )
        if _is_ancestor(parent_map, ancestor_id=job_node_id, node_id=target_node_id):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason="target is nested inside active job scope",
            )
        if _is_ancestor(parent_map, ancestor_id=target_node_id, node_id=job_node_id):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason="active job scope is nested inside target",
            )
    return None


async def _load_parent_map(
    session: AsyncSession,
    course_id: uuid.UUID,
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Load all nodes for a course and return {node_id: parent_id} map.

    Single query — O(N) where N is number of nodes in the course.
    """
    stmt = select(MaterialNode.id, MaterialNode.parent_id).where(
        MaterialNode.course_id == course_id,
    )
    result = await session.execute(stmt)
    return {row.id: row.parent_id for row in result.all()}


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


def _is_ancestor(
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    *,
    ancestor_id: uuid.UUID | None,
    node_id: uuid.UUID | None,
) -> bool:
    """Check if *ancestor_id* is an ancestor of *node_id* using in-memory map.

    Walks parent_id chain from *node_id* up to root. Returns ``False``
    if either is ``None`` (course-level cases handled by fast path).
    """
    if ancestor_id is None or node_id is None:
        return False

    current_id: uuid.UUID | None = parent_map.get(node_id)
    visited: set[uuid.UUID] = {node_id}

    while current_id is not None:
        if current_id == ancestor_id:
            return True
        if current_id in visited:
            break  # safety: cycle in data
        visited.add(current_id)
        current_id = parent_map.get(current_id)

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
