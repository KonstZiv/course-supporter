"""Conflict detection for structure generation requests.

Checks whether a new generation request overlaps with any active
(queued/running) generation job. Two scopes overlap when one is an
ancestor of the other (or they target the same node).

Overlap table (from AR-6):
    Active scope  |  New request  |  Result
    Root (all)    |  Node A       |  409 — Node A nested in root
    Node A        |  Node A1      |  409 — A1 nested in A
    Node A        |  Node B       |  202 — independent subtrees
    Node A1       |  Node A2      |  202 — siblings
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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
    root_node_id: uuid.UUID,
    target_node_id: uuid.UUID | None,
    active_jobs: list[Job],
) -> ConflictInfo | None:
    """Check if *target_node_id* overlaps with any active generation job.

    Loads all nodes under *root_node_id* via recursive CTE, builds an
    in-memory parent map, then checks ancestor relationships without
    additional DB round-trips.

    Args:
        session: DB session for loading tree nodes.
        root_node_id: Root of the material tree (parent_materialnode_id IS NULL).
        target_node_id: Target node (None = whole tree from root).
        active_jobs: Active generation jobs for the same tree.

    Returns:
        ``ConflictInfo`` for the first conflicting job, or ``None``.
    """
    parent_map = await _load_parent_map(session, root_node_id)
    target_ancestors = _ancestor_set(parent_map, target_node_id)

    for job in active_jobs:
        job_node_id = job.materialnode_id
        if _scopes_overlap_fast(target_node_id, job_node_id):
            return ConflictInfo(
                job_id=job.id,
                job_node_id=job_node_id,
                reason=_overlap_reason(target_node_id, job_node_id),
            )
        if job_node_id in target_ancestors:
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
    root_node_id: uuid.UUID,
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Load all nodes under root_node_id via recursive CTE.

    Returns {node_id: parent_materialnode_id} map.
    """
    base = select(MaterialNode.id).where(MaterialNode.id == root_node_id)
    cte = base.cte(name="subtree", recursive=True)
    recursive = select(MaterialNode.id).join(
        cte, MaterialNode.parent_materialnode_id == cte.c.id
    )
    cte = cte.union_all(recursive)

    stmt = select(MaterialNode.id, MaterialNode.parent_materialnode_id).where(
        MaterialNode.id.in_(select(cte.c.id)),
    )
    result = await session.execute(stmt)
    return {row.id: row.parent_materialnode_id for row in result.all()}


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


def _iter_ancestors(
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    node_id: uuid.UUID,
) -> Iterator[uuid.UUID]:
    """Yield ancestor IDs from *node_id* up to root (excluding itself)."""
    visited: set[uuid.UUID] = {node_id}
    current_id = parent_map.get(node_id)
    while current_id is not None:
        if current_id in visited:
            break  # safety: cycle in data
        yield current_id
        visited.add(current_id)
        current_id = parent_map.get(current_id)


def _ancestor_set(
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    node_id: uuid.UUID | None,
) -> set[uuid.UUID]:
    """Collect all ancestor IDs for *node_id* (excluding itself)."""
    if node_id is None:
        return set()
    return set(_iter_ancestors(parent_map, node_id))


def _is_ancestor(
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    *,
    ancestor_id: uuid.UUID | None,
    node_id: uuid.UUID | None,
) -> bool:
    """Check if *ancestor_id* is an ancestor of *node_id*."""
    if ancestor_id is None or node_id is None:
        return False
    return any(uid == ancestor_id for uid in _iter_ancestors(parent_map, node_id))


def _overlap_reason(
    target_node_id: uuid.UUID | None,
    job_node_id: uuid.UUID | None,
) -> str:
    """Human-readable conflict reason for fast-path overlaps."""
    if target_node_id is None and job_node_id is None:
        return "both target the entire tree"
    if target_node_id == job_node_id:
        return "both target the same node"
    if job_node_id is None:
        return "active job covers entire tree"
    # target_node_id is None
    return "new request covers entire tree"
