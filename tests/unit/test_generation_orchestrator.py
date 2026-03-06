"""Tests for two-pass DAG generation orchestrator (generate + reconcile)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.errors import (
    GenerationConflictError,
    NodeNotFoundError,
    NoReadyMaterialsError,
)
from course_supporter.generation_orchestrator import (
    GenerationPlan,
    MappingWarning,
    _collect_job_ids,
    _partition_entries,
    _post_order,
    _pre_order,
    trigger_generation,
)
from course_supporter.storage.orm import MappingValidationState

# ── Helpers ──


def _make_node(
    *,
    node_id: uuid.UUID | None = None,
    children: list[Any] | None = None,
    materials: list[Any] | None = None,
    mappings: list[Any] | None = None,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.children = children or []
    node.materials = materials or []
    node.slide_video_mappings = mappings or []
    return node


def _make_entry(
    *,
    state: str = "ready",
    entry_id: uuid.UUID | None = None,
    materialnode_id: uuid.UUID | None = None,
    source_type: str = "text",
    source_url: str = "https://example.com/doc",
    job_id: uuid.UUID | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.id = entry_id or uuid.uuid4()
    entry.materialnode_id = materialnode_id or uuid.uuid4()
    entry.state = state
    entry.source_type = source_type
    entry.source_url = source_url
    entry.job_id = job_id
    return entry


def _make_job(job_id: uuid.UUID | None = None) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.job_type = "generate_structure"
    return job


def _make_snapshot(snap_id: uuid.UUID | None = None) -> MagicMock:
    snap = MagicMock()
    snap.id = snap_id or uuid.uuid4()
    return snap


class _Deps:
    """Bundles all mock dependencies for trigger_generation."""

    def __init__(
        self,
        *,
        root_nodes: list[Any],
        active_gen_jobs: list[Any] | None = None,
        conflict: Any = None,
        enqueue_ingestion_job: MagicMock | None = None,
        enqueue_step_job: MagicMock | None = None,
        problematic_mappings: list[Any] | None = None,
    ) -> None:
        self.node_repo = AsyncMock()
        self.node_repo.get_subtree = AsyncMock(return_value=root_nodes)

        self.job_repo = AsyncMock()
        self.job_repo.get_active_generation_jobs_in_tree = AsyncMock(
            return_value=active_gen_jobs or [],
        )

        self.detect_conflict = AsyncMock(return_value=conflict)

        self.enqueue_ingestion = AsyncMock(
            return_value=enqueue_ingestion_job or _make_job(),
        )
        self.enqueue_step = AsyncMock(
            return_value=enqueue_step_job or _make_job(),
        )

        self.mapping_repo = AsyncMock()
        self.mapping_repo.get_problematic_by_node_ids = AsyncMock(
            return_value=problematic_mappings or [],
        )


async def _run(
    deps: _Deps,
    *,
    tenant_id: uuid.UUID | None = None,
    root_node_id: uuid.UUID | None = None,
    target_node_id: uuid.UUID | None = None,
    mode: str = "free",
) -> GenerationPlan:
    """Run trigger_generation with all dependencies patched."""
    tid = tenant_id or uuid.uuid4()
    rid = root_node_id or uuid.uuid4()
    session = AsyncMock()
    redis = AsyncMock()

    with (
        patch(
            "course_supporter.storage.material_node_repository.MaterialNodeRepository",
            return_value=deps.node_repo,
        ),
        patch(
            "course_supporter.storage.job_repository.JobRepository",
            return_value=deps.job_repo,
        ),
        patch(
            "course_supporter.conflict_detection.detect_conflict",
            deps.detect_conflict,
        ),
        patch(
            "course_supporter.enqueue.enqueue_ingestion",
            deps.enqueue_ingestion,
        ),
        patch(
            "course_supporter.enqueue.enqueue_step",
            deps.enqueue_step,
        ),
        patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository",
            return_value=deps.mapping_repo,
        ),
    ):
        return await trigger_generation(
            redis=redis,
            session=session,
            tenant_id=tid,
            root_node_id=rid,
            target_node_id=target_node_id,
            mode=mode,
        )


# ── _partition_entries ──


class TestPartitionEntries:
    def test_splits_ready_and_stale(self) -> None:
        """Partitions entries into stale and ready groups."""
        ready = _make_entry(state="ready")
        raw = _make_entry(state="raw")
        error = _make_entry(state="error")
        node = _make_node(materials=[ready, raw, error])
        stale, ok = _partition_entries([node])
        assert len(stale) == 2
        assert len(ok) == 1
        assert ok[0] is ready

    def test_pending_is_stale(self) -> None:
        """PENDING entries are counted as stale."""
        pending = _make_entry(state="pending", job_id=uuid.uuid4())
        node = _make_node(materials=[pending])
        stale, ready = _partition_entries([node])
        assert len(stale) == 1
        assert len(ready) == 0


# ── _post_order ──


class TestPostOrder:
    def test_single_node(self) -> None:
        """Single node returns itself."""
        root = _make_node()
        result = _post_order([root])
        assert result == [root]

    def test_children_before_parent(self) -> None:
        """Children appear before parents in post-order."""
        child1 = _make_node()
        child2 = _make_node()
        root = _make_node(children=[child1, child2])
        result = _post_order([root])
        assert result.index(child1) < result.index(root)
        assert result.index(child2) < result.index(root)

    def test_three_levels(self) -> None:
        """Three-level tree: grandchildren → children → root."""
        gc = _make_node()
        child = _make_node(children=[gc])
        root = _make_node(children=[child])
        result = _post_order([root])
        assert result == [gc, child, root]


class TestPreOrder:
    def test_single_node(self) -> None:
        root = _make_node()
        assert _pre_order([root]) == [root]

    def test_two_levels(self) -> None:
        """Pre-order: root before children."""
        child = _make_node()
        root = _make_node(children=[child])
        result = _pre_order([root])
        assert result == [root, child]

    def test_three_levels(self) -> None:
        """Pre-order: root → child → grandchild."""
        gc = _make_node()
        child = _make_node(children=[gc])
        root = _make_node(children=[child])
        result = _pre_order([root])
        assert result == [root, child, gc]


# ── trigger_generation: per-node DAG ──


class TestSingleNodeGeneration:
    async def test_single_ready_node(self) -> None:
        """Single root with READY materials -> one generation job."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        assert len(plan.generation_jobs) == 1
        assert len(plan.ingestion_jobs) == 0
        assert plan.estimated_llm_calls == 1
        deps.enqueue_step.assert_awaited_once()
        deps.enqueue_ingestion.assert_not_awaited()


class TestPerNodeDAG:
    async def test_two_level_tree(self) -> None:
        """Root with two children -> 3 gen + 1 reconcile jobs."""
        c1_entry = _make_entry(state="ready")
        c2_entry = _make_entry(state="ready")
        child1 = _make_node(materials=[c1_entry])
        child2 = _make_node(materials=[c2_entry])
        root_entry = _make_entry(state="ready")
        root = _make_node(materials=[root_entry], children=[child1, child2])

        # 3 gen jobs + 1 reconcile job = 4 total
        jobs = [_make_job() for _ in range(4)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        assert len(plan.generation_jobs) == 3
        assert len(plan.reconciliation_jobs) == 1
        assert plan.estimated_llm_calls == 4

        # First two calls: children gen (order among siblings unspecified)
        child_calls = [deps.enqueue_step.call_args_list[i].kwargs for i in range(2)]
        child_ids = {c["target_node_id"] for c in child_calls}
        assert child_ids == {child1.id, child2.id}

        # Third call: root gen depends on children's gen job IDs
        root_gen = deps.enqueue_step.call_args_list[2].kwargs
        assert root_gen["target_node_id"] == root.id
        assert root_gen["step_type"] == "generate"

        # Fourth call: root reconcile depends on root gen
        root_rec = deps.enqueue_step.call_args_list[3].kwargs
        assert root_rec["target_node_id"] == root.id
        assert root_rec["step_type"] == "reconcile"
        assert str(jobs[2].id) in root_rec["depends_on"]

    async def test_three_level_tree(self) -> None:
        """Root → Module → Lesson: 3 gen + 2 reconcile jobs."""
        lesson_entry = _make_entry(state="ready")
        lesson = _make_node(materials=[lesson_entry])
        module_entry = _make_entry(state="ready")
        module = _make_node(materials=[module_entry], children=[lesson])
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[module],
        )

        # 3 gen + 2 reconcile (root + module are non-leaf)
        jobs = [_make_job() for _ in range(5)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        assert len(plan.generation_jobs) == 3
        assert len(plan.reconciliation_jobs) == 2
        assert plan.estimated_llm_calls == 5

        # Gen pass: lesson → module → root
        calls = deps.enqueue_step.call_args_list
        assert calls[0].kwargs["target_node_id"] == lesson.id
        assert calls[1].kwargs["target_node_id"] == module.id
        assert calls[2].kwargs["target_node_id"] == root.id

        # Reconcile pass: root first, then module
        assert calls[3].kwargs["step_type"] == "reconcile"
        assert calls[3].kwargs["target_node_id"] == root.id
        assert calls[4].kwargs["step_type"] == "reconcile"
        assert calls[4].kwargs["target_node_id"] == module.id
        # Module reconcile depends on root reconcile
        assert str(jobs[3].id) in calls[4].kwargs["depends_on"]

    async def test_empty_parent_with_children(self) -> None:
        """Parent without own materials but with children still gets jobs."""
        child_entry = _make_entry(state="ready")
        child = _make_node(materials=[child_entry])
        parent = _make_node(materials=[], children=[child])

        # 2 gen + 1 reconcile (parent is non-leaf)
        jobs = [_make_job() for _ in range(3)]
        deps = _Deps(root_nodes=[parent])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        assert len(plan.generation_jobs) == 2
        assert len(plan.reconciliation_jobs) == 1

    async def test_skip_empty_subtree(self) -> None:
        """Node without materials and no children with materials is skipped."""
        empty_child = _make_node(materials=[])
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[empty_child],
        )

        # 1 gen (root) + 1 reconcile (root is non-leaf)
        jobs = [_make_job(), _make_job()]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        # Only root gets gen, empty child is skipped
        assert len(plan.generation_jobs) == 1
        # Root gets reconcile (has children, even though empty)
        assert len(plan.reconciliation_jobs) == 1


class TestStaleWithDAG:
    async def test_ingestion_deps_per_node(self) -> None:
        """Stale entries create ingestion deps for their specific node."""
        raw = _make_entry(state="raw")
        root = _make_node(materials=[raw])
        ing_job = _make_job()
        deps = _Deps(root_nodes=[root], enqueue_ingestion_job=ing_job)

        plan = await _run(deps)

        assert len(plan.ingestion_jobs) == 1
        deps.enqueue_ingestion.assert_awaited_once()
        # Generation job depends on ingestion job
        step_kwargs = deps.enqueue_step.call_args.kwargs
        assert str(ing_job.id) in step_kwargs["depends_on"]

    async def test_pending_entry_not_re_enqueued(self) -> None:
        """PENDING entries are not re-enqueued but their job IDs go to depends_on."""
        pending_jid = uuid.uuid4()
        pending = _make_entry(state="pending", job_id=pending_jid)
        root = _make_node(materials=[pending])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        deps.enqueue_ingestion.assert_not_awaited()
        assert len(plan.ingestion_jobs) == 0
        step_kwargs = deps.enqueue_step.call_args.kwargs
        assert str(pending_jid) in step_kwargs["depends_on"]


class TestErrorEntries:
    async def test_error_entries_re_enqueued(self) -> None:
        """ERROR entries get re-enqueued for ingestion."""
        error = _make_entry(state="error")
        root = _make_node(materials=[error])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        deps.enqueue_ingestion.assert_awaited_once()
        assert len(plan.ingestion_jobs) == 1


class TestConflictDetected:
    async def test_raises_conflict_error(self) -> None:
        """Active generation overlap -> GenerationConflictError."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        conflict = MagicMock()
        conflict.job_id = uuid.uuid4()
        conflict.reason = "both target the same node"
        deps = _Deps(root_nodes=[root], conflict=conflict)

        with pytest.raises(GenerationConflictError) as exc_info:
            await _run(deps)
        assert exc_info.value.conflict is conflict


class TestNodeNotFound:
    async def test_raises_node_not_found(self) -> None:
        """Non-existent target_node_id -> NodeNotFoundError."""
        root = _make_node()
        deps = _Deps(root_nodes=[root])

        with pytest.raises(NodeNotFoundError):
            await _run(deps, target_node_id=uuid.uuid4())


class TestNoMaterials:
    async def test_empty_subtree_raises(self) -> None:
        """Empty subtree -> NoReadyMaterialsError."""
        root = _make_node(materials=[])
        deps = _Deps(root_nodes=[root])

        with pytest.raises(NoReadyMaterialsError):
            await _run(deps)


# ── Mapping warnings ──


def _make_mapping_orm(
    *,
    mapping_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    slide_number: int = 1,
    validation_state: str = "pending_validation",
) -> MagicMock:
    m = MagicMock()
    m.id = mapping_id or uuid.uuid4()
    m.materialnode_id = node_id or uuid.uuid4()
    m.slide_number = slide_number
    m.validation_state = validation_state
    return m


class TestMappingWarnings:
    async def test_no_problematic_mappings(self) -> None:
        """No problematic mappings -> empty warnings."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _Deps(root_nodes=[root], problematic_mappings=[])

        plan = await _run(deps)

        assert plan.mapping_warnings == []

    async def test_pending_mapping_in_warnings(self) -> None:
        """Pending validation mapping appears in warnings."""
        entry = _make_entry(state="ready")
        node_id = uuid.uuid4()
        root = _make_node(node_id=node_id, materials=[entry])
        pending = _make_mapping_orm(
            node_id=node_id,
            slide_number=3,
            validation_state="pending_validation",
        )
        deps = _Deps(root_nodes=[root], problematic_mappings=[pending])

        plan = await _run(deps)

        assert len(plan.mapping_warnings) == 1
        w = plan.mapping_warnings[0]
        assert isinstance(w, MappingWarning)
        assert w.mapping_id == pending.id
        assert w.materialnode_id == node_id
        assert w.slide_number == 3
        assert w.validation_state == MappingValidationState.PENDING_VALIDATION

    async def test_failed_mapping_in_warnings(self) -> None:
        """Validation failed mapping appears in warnings."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        failed = _make_mapping_orm(validation_state="validation_failed")
        deps = _Deps(root_nodes=[root], problematic_mappings=[failed])

        plan = await _run(deps)

        assert len(plan.mapping_warnings) == 1
        warn = plan.mapping_warnings[0]
        assert warn.validation_state == MappingValidationState.VALIDATION_FAILED

    async def test_warnings_present_in_cascade_plan(self) -> None:
        """Warnings are included in cascade (stale materials) plans."""
        raw = _make_entry(state="raw")
        root = _make_node(materials=[raw])
        failed = _make_mapping_orm(validation_state="validation_failed")
        deps = _Deps(root_nodes=[root], problematic_mappings=[failed])

        plan = await _run(deps)

        assert len(plan.ingestion_jobs) == 1
        assert len(plan.mapping_warnings) == 1


# ── _collect_job_ids ──


class TestCollectPendingJobIds:
    def test_mixed_entries(self) -> None:
        """Returns only IDs from entries with job_id."""
        jid1 = uuid.uuid4()
        jid2 = uuid.uuid4()
        e1 = _make_entry(state="pending", job_id=jid1)
        e2 = _make_entry(state="raw")
        e3 = _make_entry(state="pending", job_id=jid2)

        result = _collect_job_ids([e1, e2, e3])

        assert result == [str(jid1), str(jid2)]

    def test_no_job_ids(self) -> None:
        """All entries without job_id returns empty list."""
        e1 = _make_entry(state="raw")
        e2 = _make_entry(state="error")

        result = _collect_job_ids([e1, e2])

        assert result == []

    def test_empty_input(self) -> None:
        """Empty input returns empty list."""
        assert _collect_job_ids([]) == []


class TestDAGEndToEnd:
    """Comprehensive DAG scenarios."""

    async def test_wide_tree_with_mixed_materials(self) -> None:
        """Wide tree: root has 3 children, 2 with materials, 1 empty."""
        c1 = _make_node(materials=[_make_entry(state="ready")])
        c2 = _make_node(materials=[_make_entry(state="ready")])
        c3_empty = _make_node(materials=[])
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[c1, c2, c3_empty],
        )

        # 3 gen + 1 reconcile (root is non-leaf)
        jobs = [_make_job() for _ in range(4)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        assert len(plan.generation_jobs) == 3
        assert len(plan.reconciliation_jobs) == 1

    async def test_stale_child_creates_ingestion_then_generation(self) -> None:
        """Child with raw material: ingestion + gen, parent depends on child."""
        raw = _make_entry(state="raw")
        child = _make_node(materials=[raw])
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[child],
        )

        ing_job = _make_job()
        # 2 gen + 1 reconcile (root is non-leaf)
        step_jobs = [_make_job() for _ in range(3)]
        deps = _Deps(root_nodes=[root], enqueue_ingestion_job=ing_job)
        deps.enqueue_step = AsyncMock(side_effect=step_jobs)

        plan = await _run(deps)

        assert len(plan.ingestion_jobs) == 1
        assert len(plan.generation_jobs) == 2
        assert len(plan.reconciliation_jobs) == 1
        # Child gen depends on its ingestion
        child_call = deps.enqueue_step.call_args_list[0].kwargs
        assert str(ing_job.id) in child_call["depends_on"]

    async def test_subtree_generation(self) -> None:
        """Target subtree: only target and descendants get jobs."""
        leaf = _make_node(materials=[_make_entry(state="ready")])
        target = _make_node(
            materials=[_make_entry(state="ready")],
            children=[leaf],
        )
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[target],
        )

        # 2 gen + 1 reconcile (target is non-leaf)
        jobs = [_make_job() for _ in range(3)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps, target_node_id=target.id)

        assert len(plan.generation_jobs) == 2
        assert len(plan.reconciliation_jobs) == 1
        gen_ids = {
            c.kwargs["target_node_id"]
            for c in deps.enqueue_step.call_args_list
            if c.kwargs["step_type"] == "generate"
        }
        assert gen_ids == {leaf.id, target.id}


class TestReconcilePass:
    """Reconciliation pass: top-down, non-leaf only."""

    async def test_leaf_only_no_reconcile(self) -> None:
        """Single leaf node → no reconciliation jobs."""
        root = _make_node(materials=[_make_entry(state="ready")])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        assert len(plan.reconciliation_jobs) == 0

    async def test_reconcile_depends_on_root_gen(self) -> None:
        """First reconcile job depends on root generate job."""
        child = _make_node(materials=[_make_entry(state="ready")])
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[child],
        )

        # 2 gen + 1 reconcile
        jobs = [_make_job() for _ in range(3)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        await _run(deps)

        rec_call = deps.enqueue_step.call_args_list[2].kwargs
        assert rec_call["step_type"] == "reconcile"
        # Root gen is jobs[1] (second gen call)
        assert str(jobs[1].id) in rec_call["depends_on"]

    async def test_reconcile_chain_top_down(self) -> None:
        """Reconcile jobs form a top-down chain."""
        leaf = _make_node(materials=[_make_entry(state="ready")])
        mid = _make_node(
            materials=[_make_entry(state="ready")],
            children=[leaf],
        )
        root = _make_node(
            materials=[_make_entry(state="ready")],
            children=[mid],
        )

        # 3 gen + 2 reconcile (root + mid are non-leaf)
        jobs = [_make_job() for _ in range(5)]
        deps = _Deps(root_nodes=[root])
        deps.enqueue_step = AsyncMock(side_effect=jobs)

        plan = await _run(deps)

        assert len(plan.reconciliation_jobs) == 2
        calls = deps.enqueue_step.call_args_list
        # reconcile_root = calls[3], reconcile_mid = calls[4]
        rec_root = calls[3].kwargs
        rec_mid = calls[4].kwargs
        assert rec_root["step_type"] == "reconcile"
        assert rec_mid["step_type"] == "reconcile"
        # Mid reconcile depends on root reconcile
        assert str(jobs[3].id) in rec_mid["depends_on"]


# ── GenerationPlan backward compat ──


class TestGenerationPlanCompat:
    def test_generation_job_property(self) -> None:
        """generation_job returns first job from list."""
        j = _make_job()
        plan = GenerationPlan(generation_jobs=[j])
        assert plan.generation_job is j

    def test_generation_job_none_when_empty(self) -> None:
        """generation_job returns None for empty list."""
        plan = GenerationPlan()
        assert plan.generation_job is None
