"""Cascade generation orchestrator.

Detects stale materials, enqueues ingestion for them, then enqueues
structure generation with ``depends_on`` linking to ingestion jobs.
If all materials are READY, performs idempotency check before
enqueuing generation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from course_supporter.storage.orm import (
        Job,
        MappingValidationState,
        MaterialEntry,
        MaterialNode,
    )


@dataclass(frozen=True, slots=True)
class MappingWarning:
    """Warning about a slide-video mapping with problematic validation state.

    Non-blocking: does not prevent generation, only informs the user.
    """

    mapping_id: uuid.UUID
    materialnode_id: uuid.UUID
    slide_number: int
    validation_state: MappingValidationState


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    """Result of cascade generation orchestration.

    Attributes:
        ingestion_jobs: Jobs created for stale material ingestion.
        generation_jobs: Per-node generation jobs (bottom-up DAG order).
        existing_snapshot_id: Existing snapshot UUID if idempotent.
        is_idempotent: True when no new work is needed.
        mapping_warnings: Mappings with pending/failed validation states.
        estimated_llm_calls: Total LLM calls expected for this plan.
    """

    ingestion_jobs: list[Job] = field(default_factory=list)
    generation_jobs: list[Job] = field(default_factory=list)
    existing_snapshot_id: uuid.UUID | None = None
    is_idempotent: bool = False
    mapping_warnings: list[MappingWarning] = field(default_factory=list)
    estimated_llm_calls: int = 0

    @property
    def generation_job(self) -> Job | None:
        """First generation job, for backward compatibility."""
        return self.generation_jobs[0] if self.generation_jobs else None


def _partition_entries(
    flat_nodes: list[MaterialNode],
) -> tuple[list[MaterialEntry], list[MaterialEntry]]:
    """Split all entries into (stale, ready) based on MaterialState.

    PENDING entries are counted as stale (ingestion in-flight).

    Args:
        flat_nodes: Flat list of nodes with materials loaded.

    Returns:
        Tuple of (stale_entries, ready_entries).
    """
    from course_supporter.storage.orm import MaterialState

    stale: list[MaterialEntry] = []
    ready: list[MaterialEntry] = []
    for node in flat_nodes:
        for entry in node.materials:
            if entry.state == MaterialState.READY:
                ready.append(entry)
            else:
                stale.append(entry)
    return stale, ready


def _collect_job_ids(
    stale: list[MaterialEntry],
) -> list[str]:
    """Collect Job UUIDs (as str) for PENDING entries.

    PENDING entries already have an in-flight ingestion job.
    We need their Job IDs for the generation ``depends_on`` list.

    Args:
        stale: Stale entries (may include PENDING ones).

    Returns:
        List of Job UUID strings for entries with job_id.
    """
    return [str(entry.job_id) for entry in stale if entry.job_id is not None]


async def _collect_mapping_warnings(
    session: AsyncSession,
    flat_nodes: list[MaterialNode],
) -> list[MappingWarning]:
    """Collect non-blocking warnings about problematic slide-video mappings.

    Args:
        session: Active DB session.
        flat_nodes: Flat list of nodes in the target subtree.

    Returns:
        List of warnings for pending/failed validation mappings.
    """
    from course_supporter.storage.orm import MappingValidationState
    from course_supporter.storage.repositories import SlideVideoMappingRepository

    mapping_repo = SlideVideoMappingRepository(session)
    node_ids = [n.id for n in flat_nodes]
    problematic = await mapping_repo.get_problematic_by_node_ids(node_ids)
    return [
        MappingWarning(
            mapping_id=m.id,
            materialnode_id=m.materialnode_id,
            slide_number=m.slide_number,
            validation_state=MappingValidationState(m.validation_state),
        )
        for m in problematic
    ]


def _post_order(nodes: list[MaterialNode]) -> list[MaterialNode]:
    """Flatten tree nodes in post-order (children before parents).

    Uses iterative traversal to avoid recursion limits.
    """
    order: list[MaterialNode] = []
    stack: list[MaterialNode] = list(reversed(nodes))
    while stack:
        node = stack.pop()
        order.append(node)
        for child in reversed(node.children):
            stack.append(child)
    order.reverse()
    return order


def _node_has_content(
    node: MaterialNode,
    children_with_jobs: set[uuid.UUID],
) -> bool:
    """Check if a node should get a generation job.

    A node needs generation if it has own materials (any state)
    or at least one child already has a generation job.
    """
    return bool(node.materials) or bool(
        {c.id for c in node.children} & children_with_jobs
    )


async def trigger_generation(
    *,
    redis: ArqRedis,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    root_node_id: uuid.UUID,
    target_node_id: uuid.UUID | None = None,
    mode: Literal["free", "guided"] = "free",
) -> GenerationPlan:
    """Orchestrate per-node bottom-up generation DAG.

    Builds a DAG of per-node generation jobs in post-order
    (leaves first, parents wait on children). Each node's
    generation depends on its own ingestion jobs plus its
    children's generation jobs.

    The caller is responsible for committing the session.

    Args:
        redis: ARQ Redis connection pool.
        session: Active DB session (caller controls transaction).
        tenant_id: Owning tenant UUID.
        root_node_id: Root MaterialNode UUID of the tree.
        target_node_id: Specific subtree node UUID (None = whole tree).
        mode: Generation mode ('free' or 'guided').

    Returns:
        GenerationPlan describing the enqueued work.

    Raises:
        NodeNotFoundError: If target_node_id is given but not found.
        GenerationConflictError: If an active generation overlaps.
        NoReadyMaterialsError: If subtree has no materials at all.
    """
    from course_supporter.conflict_detection import detect_conflict
    from course_supporter.enqueue import enqueue_ingestion, enqueue_step
    from course_supporter.errors import (
        GenerationConflictError,
        NoReadyMaterialsError,
    )
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )
    from course_supporter.storage.orm import MaterialState
    from course_supporter.tree_utils import resolve_target_nodes

    log = structlog.get_logger().bind(
        root_node_id=str(root_node_id),
        target_node_id=str(target_node_id),
        mode=mode,
    )
    log.info("trigger_generation_started")

    # 1. Load tree and resolve target
    node_repo = MaterialNodeRepository(session)
    root_nodes: list[MaterialNode] = await node_repo.get_subtree(
        root_node_id,
        include_materials=True,
    )
    target, flat_nodes = resolve_target_nodes(root_nodes, target_node_id)

    # 1b. Collect mapping warnings (non-blocking)
    mapping_warnings = await _collect_mapping_warnings(session, flat_nodes)

    # 2. Conflict detection
    job_repo = JobRepository(session)
    all_tree_node_ids = [n.id for n in flat_nodes]
    from course_supporter.tree_utils import flatten_subtree

    if target_node_id is not None and root_nodes:
        all_tree_flat: list[MaterialNode] = []
        for rn in root_nodes:
            all_tree_flat.extend(flatten_subtree(rn))
        all_tree_node_ids = [n.id for n in all_tree_flat]

    active_gen_jobs = await job_repo.get_active_generation_jobs_in_tree(
        all_tree_node_ids
    )
    conflict = await detect_conflict(
        session,
        root_node_id,
        target_node_id,
        active_gen_jobs,
    )
    if conflict is not None:
        raise GenerationConflictError(conflict)

    # 3. Check there are any materials at all
    stale, ready = _partition_entries(flat_nodes)
    if not stale and not ready:
        msg = "No materials found in target subtree"
        raise NoReadyMaterialsError(msg)

    # 4. Build per-node DAG in post-order (bottom-up)
    target_roots = [target] if target is not None else root_nodes
    processing_order = _post_order(target_roots)

    ingestion_jobs: list[Job] = []
    generation_jobs: list[Job] = []
    node_gen_jobs: dict[uuid.UUID, Job] = {}

    for node in processing_order:
        # Per-node ingestion dependencies
        node_deps: list[str] = []
        for entry in node.materials:
            if entry.state == MaterialState.READY:
                continue
            if entry.job_id is not None:
                node_deps.append(str(entry.job_id))
            else:
                ing_job = await enqueue_ingestion(
                    redis=redis,
                    session=session,
                    tenant_id=tenant_id,
                    node_id=node.id,
                    material_id=entry.id,
                    source_type=entry.source_type,
                    source_url=entry.source_url,
                )
                ingestion_jobs.append(ing_job)
                node_deps.append(str(ing_job.id))

        # Children generation dependencies
        children_deps = [
            str(node_gen_jobs[child.id].id)
            for child in node.children
            if child.id in node_gen_jobs
        ]
        node_deps.extend(children_deps)

        # Skip nodes with no content (no materials, no children with jobs)
        nodes_with_jobs = set(node_gen_jobs.keys())
        if not _node_has_content(node, nodes_with_jobs):
            continue

        gen_job = await enqueue_step(
            redis=redis,
            session=session,
            tenant_id=tenant_id,
            root_node_id=root_node_id,
            target_node_id=node.id,
            mode=mode,
            step_type="generate",
            depends_on=node_deps if node_deps else None,
        )
        generation_jobs.append(gen_job)
        node_gen_jobs[node.id] = gen_job

    log.info(
        "trigger_generation_dag_built",
        generation_jobs_count=len(generation_jobs),
        ingestion_jobs_count=len(ingestion_jobs),
    )
    return GenerationPlan(
        ingestion_jobs=ingestion_jobs,
        generation_jobs=generation_jobs,
        mapping_warnings=mapping_warnings,
        estimated_llm_calls=len(generation_jobs),
    )
