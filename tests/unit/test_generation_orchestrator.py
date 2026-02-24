"""Tests for cascade generation orchestrator."""

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
    _partition_entries,
    trigger_generation,
)

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
    source_type: str = "text",
    source_url: str = "https://example.com/doc",
    pending_job_id: uuid.UUID | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.id = entry_id or uuid.uuid4()
    entry.state = state
    entry.source_type = source_type
    entry.source_url = source_url
    entry.pending_job_id = pending_job_id
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
        find_identity: Any = None,
        fingerprint: str = "a" * 64,
        enqueue_ingestion_job: MagicMock | None = None,
        enqueue_generation_job: MagicMock | None = None,
        problematic_mappings: list[Any] | None = None,
    ) -> None:
        # MaterialNodeRepository
        self.node_repo = AsyncMock()
        self.node_repo.get_tree = AsyncMock(return_value=root_nodes)

        # JobRepository
        self.job_repo = AsyncMock()
        self.job_repo.get_active_generation_jobs = AsyncMock(
            return_value=active_gen_jobs or [],
        )

        # detect_conflict
        self.detect_conflict = AsyncMock(return_value=conflict)

        # FingerprintService
        self.fp_service = AsyncMock()
        self.fp_service.ensure_node_fp = AsyncMock(return_value=fingerprint)
        self.fp_service.ensure_course_fp = AsyncMock(return_value=fingerprint)

        # SnapshotRepository
        self.snap_repo = AsyncMock()
        self.snap_repo.find_by_identity = AsyncMock(return_value=find_identity)

        # enqueue helpers
        self.enqueue_ingestion = AsyncMock(
            return_value=enqueue_ingestion_job or _make_job(),
        )
        self.enqueue_generation = AsyncMock(
            return_value=enqueue_generation_job or _make_job(),
        )

        # SlideVideoMappingRepository
        self.mapping_repo = AsyncMock()
        self.mapping_repo.get_problematic_by_node_ids = AsyncMock(
            return_value=problematic_mappings or [],
        )


async def _run(
    deps: _Deps,
    *,
    course_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    mode: str = "free",
) -> GenerationPlan:
    """Run trigger_generation with all dependencies patched."""
    cid = course_id or uuid.uuid4()
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
            "course_supporter.fingerprint.FingerprintService",
            return_value=deps.fp_service,
        ),
        patch(
            "course_supporter.storage.snapshot_repository.SnapshotRepository",
            return_value=deps.snap_repo,
        ),
        patch(
            "course_supporter.enqueue.enqueue_ingestion",
            deps.enqueue_ingestion,
        ),
        patch(
            "course_supporter.enqueue.enqueue_generation",
            deps.enqueue_generation,
        ),
        patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository",
            return_value=deps.mapping_repo,
        ),
    ):
        return await trigger_generation(
            redis=redis,
            session=session,
            course_id=cid,
            node_id=node_id,
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
        pending = _make_entry(state="pending", pending_job_id=uuid.uuid4())
        node = _make_node(materials=[pending])
        stale, ready = _partition_entries([node])
        assert len(stale) == 1
        assert len(ready) == 0


# ── trigger_generation ──


class TestAllReadyNoSnapshot:
    @pytest.mark.asyncio
    async def test_enqueues_generation(self) -> None:
        """All READY, no existing snapshot → generation job enqueued."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        assert not plan.is_idempotent
        assert plan.generation_job is not None
        assert len(plan.ingestion_jobs) == 0
        deps.enqueue_generation.assert_awaited_once()
        deps.enqueue_ingestion.assert_not_awaited()


class TestAllReadyIdempotent:
    @pytest.mark.asyncio
    async def test_returns_existing_snapshot(self) -> None:
        """All READY, snapshot exists → idempotent, no jobs."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        snap = _make_snapshot()
        deps = _Deps(root_nodes=[root], find_identity=snap)

        plan = await _run(deps)

        assert plan.is_idempotent
        assert plan.existing_snapshot_id == snap.id
        assert plan.generation_job is None
        deps.enqueue_generation.assert_not_awaited()


class TestStalePresent:
    @pytest.mark.asyncio
    async def test_enqueues_ingestion_and_generation(self) -> None:
        """Stale (RAW) entries → ingestion + generation with depends_on."""
        raw = _make_entry(state="raw")
        ready = _make_entry(state="ready")
        root = _make_node(materials=[raw, ready])
        ing_job = _make_job()
        gen_job = _make_job()
        deps = _Deps(
            root_nodes=[root],
            enqueue_ingestion_job=ing_job,
            enqueue_generation_job=gen_job,
        )

        plan = await _run(deps)

        assert not plan.is_idempotent
        assert len(plan.ingestion_jobs) == 1
        assert plan.generation_job is gen_job
        deps.enqueue_ingestion.assert_awaited_once()
        # depends_on should contain ingestion job ID
        gen_kwargs = deps.enqueue_generation.call_args.kwargs
        assert str(ing_job.id) in gen_kwargs["depends_on"]


class TestPendingEntries:
    @pytest.mark.asyncio
    async def test_no_re_enqueue_for_pending(self) -> None:
        """PENDING entries are not re-enqueued but their job IDs go to depends_on."""
        pending_jid = uuid.uuid4()
        pending = _make_entry(
            state="pending",
            pending_job_id=pending_jid,
        )
        root = _make_node(materials=[pending])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        # No new ingestion job enqueued (was pending)
        deps.enqueue_ingestion.assert_not_awaited()
        assert len(plan.ingestion_jobs) == 0
        # But depends_on includes the pending job ID
        gen_kwargs = deps.enqueue_generation.call_args.kwargs
        assert str(pending_jid) in gen_kwargs["depends_on"]


class TestErrorEntries:
    @pytest.mark.asyncio
    async def test_error_entries_re_enqueued(self) -> None:
        """ERROR entries get re-enqueued for ingestion."""
        error = _make_entry(state="error")
        root = _make_node(materials=[error])
        deps = _Deps(root_nodes=[root])

        plan = await _run(deps)

        deps.enqueue_ingestion.assert_awaited_once()
        assert len(plan.ingestion_jobs) == 1


class TestConflictDetected:
    @pytest.mark.asyncio
    async def test_raises_conflict_error(self) -> None:
        """Active generation overlap → GenerationConflictError."""
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
    @pytest.mark.asyncio
    async def test_raises_node_not_found(self) -> None:
        """Non-existent node_id → NodeNotFoundError."""
        root = _make_node()
        deps = _Deps(root_nodes=[root])

        with pytest.raises(NodeNotFoundError):
            await _run(deps, node_id=uuid.uuid4())


class TestCourseLevelFingerprint:
    @pytest.mark.asyncio
    async def test_uses_course_fingerprint(self) -> None:
        """Course-level (node_id=None) uses ensure_course_fp."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _Deps(root_nodes=[root])

        await _run(deps, node_id=None)

        deps.fp_service.ensure_course_fp.assert_awaited_once()
        deps.fp_service.ensure_node_fp.assert_not_awaited()


class TestNoMaterials:
    @pytest.mark.asyncio
    async def test_empty_subtree_raises(self) -> None:
        """Empty subtree → NoReadyMaterialsError."""
        root = _make_node(materials=[])
        deps = _Deps(root_nodes=[root])

        with pytest.raises(NoReadyMaterialsError):
            await _run(deps)


def _make_mapping_orm(
    *,
    mapping_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    slide_number: int = 1,
    validation_state: str = "pending_validation",
) -> MagicMock:
    """Create a mock SlideVideoMapping."""
    m = MagicMock()
    m.id = mapping_id or uuid.uuid4()
    m.node_id = node_id or uuid.uuid4()
    m.slide_number = slide_number
    m.validation_state = validation_state
    return m


class TestMappingWarnings:
    @pytest.mark.asyncio
    async def test_no_problematic_mappings(self) -> None:
        """No problematic mappings → empty warnings."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _Deps(root_nodes=[root], problematic_mappings=[])

        plan = await _run(deps)

        assert plan.mapping_warnings == []

    @pytest.mark.asyncio
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
        assert w.node_id == node_id
        assert w.slide_number == 3
        assert w.validation_state == "pending_validation"

    @pytest.mark.asyncio
    async def test_failed_mapping_in_warnings(self) -> None:
        """Validation failed mapping appears in warnings."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        failed = _make_mapping_orm(validation_state="validation_failed")
        deps = _Deps(root_nodes=[root], problematic_mappings=[failed])

        plan = await _run(deps)

        assert len(plan.mapping_warnings) == 1
        assert plan.mapping_warnings[0].validation_state == "validation_failed"

    @pytest.mark.asyncio
    async def test_warnings_present_in_idempotent_plan(self) -> None:
        """Warnings are included even for idempotent (snapshot exists) plans."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        snap = _make_snapshot()
        pending = _make_mapping_orm(validation_state="pending_validation")
        deps = _Deps(
            root_nodes=[root],
            find_identity=snap,
            problematic_mappings=[pending],
        )

        plan = await _run(deps)

        assert plan.is_idempotent
        assert len(plan.mapping_warnings) == 1

    @pytest.mark.asyncio
    async def test_warnings_present_in_cascade_plan(self) -> None:
        """Warnings are included in cascade (stale materials) plans."""
        raw = _make_entry(state="raw")
        root = _make_node(materials=[raw])
        failed = _make_mapping_orm(validation_state="validation_failed")
        deps = _Deps(root_nodes=[root], problematic_mappings=[failed])

        plan = await _run(deps)

        assert len(plan.ingestion_jobs) == 1
        assert len(plan.mapping_warnings) == 1
