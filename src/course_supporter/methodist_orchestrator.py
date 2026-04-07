"""Methodist orchestrator: builds a two-pass DAG of per-node Methodist jobs.

Pass 1 (bottom-up): post-order traversal — leaves first so that parent
nodes can reference children's Methodist results.

Pass 2 (top-down): pre-order traversal — root first for consistency
checks and cross-node gap analysis.

Operates on StructureNodeEditable trees (created after generation).
Requires that the node has an editable tree before Methodist can run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from course_supporter.storage.orm import Job, StructureNodeEditable


@dataclass(frozen=True, slots=True)
class MethodistPlan:
    """Result of Methodist orchestration.

    Attributes:
        bottom_up_jobs: Per-node Methodist jobs (leaves first).
        top_down_jobs: Per-node consistency jobs (root first).
        estimated_llm_calls: Total LLM calls expected.
    """

    bottom_up_jobs: list[Job] = field(default_factory=list)
    top_down_jobs: list[Job] = field(default_factory=list)
    estimated_llm_calls: int = 0


def _post_order(
    nodes: list[StructureNodeEditable],
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    children_map: dict[uuid.UUID, list[uuid.UUID]],
) -> list[uuid.UUID]:
    """Return node IDs in post-order (children before parents)."""
    visited: set[uuid.UUID] = set()
    order: list[uuid.UUID] = []

    def _visit(nid: uuid.UUID) -> None:
        if nid in visited:
            return
        visited.add(nid)
        for child_id in children_map.get(nid, []):
            _visit(child_id)
        order.append(nid)

    roots = [nid for nid, pid in parent_map.items() if pid is None]
    for root_id in roots:
        _visit(root_id)
    return order


def _pre_order(
    nodes: list[StructureNodeEditable],
    parent_map: dict[uuid.UUID, uuid.UUID | None],
    children_map: dict[uuid.UUID, list[uuid.UUID]],
) -> list[uuid.UUID]:
    """Return node IDs in pre-order (parents before children)."""
    order: list[uuid.UUID] = []
    roots = [nid for nid, pid in parent_map.items() if pid is None]
    stack = list(reversed(roots))
    while stack:
        nid = stack.pop()
        order.append(nid)
        for child_id in reversed(children_map.get(nid, [])):
            stack.append(child_id)
    return order


def _build_maps(
    editables: list[StructureNodeEditable],
) -> tuple[
    dict[uuid.UUID, uuid.UUID | None],
    dict[uuid.UUID, list[uuid.UUID]],
]:
    """Build parent_map and children_map from flat editable list."""
    parent_map: dict[uuid.UUID, uuid.UUID | None] = {}
    children_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    for ed in editables:
        parent_map[ed.id] = ed.parent_editable_id
        children_map.setdefault(ed.id, [])
        if ed.parent_editable_id is not None:
            children_map.setdefault(ed.parent_editable_id, [])
            children_map[ed.parent_editable_id].append(ed.id)
    return parent_map, children_map


async def trigger_methodist(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    materialnode_id: uuid.UUID,
) -> MethodistPlan:
    """Orchestrate two-pass Methodist DAG over editable tree.

    Pass 1 (bottom-up): post-order — each node's Methodist step
    depends on its children's Methodist completion.

    Pass 2 (top-down): pre-order — each node depends on its
    parent's top-down step and its own bottom-up step.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        materialnode_id: Root MaterialNode UUID (must have editables).

    Returns:
        MethodistPlan describing the enqueued work.

    Raises:
        ValueError: If no editable tree exists for this node.
    """
    from course_supporter.storage.editable_repository import (
        EditableRepository,
    )

    log = structlog.get_logger().bind(
        materialnode_id=str(materialnode_id),
    )
    log.info("trigger_methodist_started")

    # 1. Load editable tree
    editable_repo = EditableRepository(session)
    editables = await editable_repo.get_tree(materialnode_id)
    if not editables:
        msg = (
            f"No editable tree found for MaterialNode "
            f"{materialnode_id}. Run generation first."
        )
        raise ValueError(msg)

    parent_map, children_map = _build_maps(editables)

    # 2. Pass 1: bottom-up (post-order)
    post_ids = _post_order(editables, parent_map, children_map)
    node_bu_jobs: dict[uuid.UUID, Job] = {}
    bottom_up_jobs: list[Job] = []

    for nid in post_ids:
        deps: list[str] = []
        for child_id in children_map.get(nid, []):
            if child_id in node_bu_jobs:
                deps.append(str(node_bu_jobs[child_id].id))

        job = await _enqueue_methodist_step(
            redis=redis,
            session=session,
            tenant_id=tenant_id,
            materialnode_id=materialnode_id,
            editable_id=nid,
            pass_type="bottom_up",  # noqa: S106
            depends_on=deps if deps else None,
        )
        node_bu_jobs[nid] = job
        bottom_up_jobs.append(job)

    # 3. Pass 2: top-down (pre-order) for non-leaf nodes
    pre_ids = _pre_order(editables, parent_map, children_map)
    node_td_jobs: dict[uuid.UUID, Job] = {}
    top_down_jobs: list[Job] = []

    for nid in pre_ids:
        # Only non-leaf nodes need top-down consistency pass
        if not children_map.get(nid):
            continue

        deps = []
        # Depends on own bottom-up completion
        if nid in node_bu_jobs:
            deps.append(str(node_bu_jobs[nid].id))
        # Depends on parent's top-down step
        parent_id = parent_map.get(nid)
        if parent_id is not None and parent_id in node_td_jobs:
            deps.append(str(node_td_jobs[parent_id].id))

        job = await _enqueue_methodist_step(
            redis=redis,
            session=session,
            tenant_id=tenant_id,
            materialnode_id=materialnode_id,
            editable_id=nid,
            pass_type="top_down",  # noqa: S106
            depends_on=deps if deps else None,
        )
        node_td_jobs[nid] = job
        top_down_jobs.append(job)

    total = len(bottom_up_jobs) + len(top_down_jobs)
    log.info(
        "trigger_methodist_dag_built",
        bottom_up_count=len(bottom_up_jobs),
        top_down_count=len(top_down_jobs),
        total_llm_calls=total,
    )
    return MethodistPlan(
        bottom_up_jobs=bottom_up_jobs,
        top_down_jobs=top_down_jobs,
        estimated_llm_calls=total,
    )


async def _enqueue_methodist_step(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    materialnode_id: uuid.UUID,
    editable_id: uuid.UUID,
    pass_type: Literal["bottom_up", "top_down"],
    depends_on: list[str] | None = None,
) -> Job:
    """Create a Job record and enqueue a Methodist step to ARQ."""
    from course_supporter.storage.job_repository import JobRepository

    log = structlog.get_logger().bind(
        editable_id=str(editable_id),
        pass_type=pass_type,
    )
    repo = JobRepository(session)

    validated_deps: list[str] | None = None
    if depends_on:
        validated_deps = [str(uuid.UUID(dep)) for dep in depends_on]

    job = await repo.create(
        tenant_id=tenant_id,
        materialnode_id=materialnode_id,
        job_type=f"methodist_{pass_type}",
        depends_on=validated_deps,
        input_params={
            "materialnode_id": str(materialnode_id),
            "editable_id": str(editable_id),
            "pass_type": pass_type,
        },
    )

    arq_job = await redis.enqueue_job(
        "arq_execute_methodist_step",
        str(job.id),
        str(materialnode_id),
        str(editable_id),
        pass_type,
    )

    if arq_job is not None:
        await repo.set_arq_job_id(job.id, arq_job.job_id)

    log.info(
        "methodist_step_enqueued",
        job_id=str(job.id),
        depends_on_count=len(validated_deps) if validated_deps else 0,
        arq_job_id=arq_job.job_id if arq_job else None,
    )
    return job
