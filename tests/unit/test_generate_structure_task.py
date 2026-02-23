"""Tests for arq_generate_structure background task."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.agents.architect import GenerationResult
from course_supporter.api.tasks import arq_generate_structure
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseStructure, ModuleOutput
from course_supporter.tree_utils import find_node_bfs, flatten_subtree

# ── Helpers ──


def _make_node(
    *,
    node_id: uuid.UUID | None = None,
    children: list[Any] | None = None,
    materials: list[Any] | None = None,
    mappings: list[Any] | None = None,
    node_fingerprint: str | None = None,
) -> MagicMock:
    """Create a mock MaterialNode with required attributes."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.children = children or []
    node.materials = materials or []
    node.slide_video_mappings = mappings or []
    node.node_fingerprint = node_fingerprint
    return node


def _make_entry(
    *,
    state: str = "ready",
    processed_content: str | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.state = state
    entry.processed_content = processed_content or (
        '{"source_type": "text", "source_url": "file:///test.md"}'
    )
    return entry


def _make_mapping(
    *,
    validation_state: str = "validated",
    slide_number: int = 1,
    video_timecode_start: str = "00:01:00",
) -> MagicMock:
    """Create a mock SlideVideoMapping."""
    m = MagicMock()
    m.validation_state = validation_state
    m.slide_number = slide_number
    m.video_timecode_start = video_timecode_start
    return m


def _make_snapshot(snapshot_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock CourseStructureSnapshot."""
    snap = MagicMock()
    snap.id = snapshot_id or uuid.uuid4()
    return snap


def _sample_gen_result() -> GenerationResult:
    """Create a sample GenerationResult for mocking ArchitectAgent."""
    structure = CourseStructure(
        title="Generated Course",
        modules=[ModuleOutput(title="Module 1")],
    )
    response = LLMResponse(
        content="{}",
        provider="gemini",
        model_id="gemini-2.5-flash",
        tokens_in=100,
        tokens_out=200,
        cost_usd=0.005,
    )
    return GenerationResult(
        structure=structure,
        prompt_version="v1",
        response=response,
    )


# ── flatten_subtree / find_node_bfs ──


class TestFlattenSubtree:
    def test_single_node(self) -> None:
        """Single node without children returns list of one."""
        root = _make_node()
        assert flatten_subtree(root) == [root]

    def test_nested_tree(self) -> None:
        """Collects all descendants via BFS."""
        child1 = _make_node()
        child2 = _make_node()
        grandchild = _make_node()
        child1.children = [grandchild]
        root = _make_node(children=[child1, child2])
        result = flatten_subtree(root)
        assert len(result) == 4
        assert result[0] is root
        assert grandchild in result


class TestFindNodeBfs:
    def test_finds_target(self) -> None:
        """Finds node by ID in nested tree."""
        target_id = uuid.uuid4()
        target = _make_node(node_id=target_id)
        root = _make_node(children=[_make_node(children=[target])])
        assert find_node_bfs([root], target_id) is target

    def test_returns_none_for_missing(self) -> None:
        """Returns None when node not found."""
        root = _make_node()
        assert find_node_bfs([root], uuid.uuid4()) is None


# ── arq_generate_structure ──


@pytest.fixture()
def job_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def course_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def node_id_str() -> str:
    return str(uuid.uuid4())


class _MockDeps:
    """Holds all mock dependencies for arq_generate_structure."""

    def __init__(
        self,
        *,
        root_nodes: list[Any],
        find_identity: Any = None,
        gen_result: GenerationResult | None = None,
        created_snapshot: Any = None,
        fingerprint: str = "a" * 64,
    ) -> None:
        # JobRepository
        self.job_repo = AsyncMock()

        # MaterialNodeRepository
        self.node_repo = AsyncMock()
        self.node_repo.get_tree = AsyncMock(return_value=root_nodes)

        # FingerprintService
        self.fp_service = AsyncMock()
        self.fp_service.ensure_node_fp = AsyncMock(return_value=fingerprint)
        self.fp_service.ensure_course_fp = AsyncMock(return_value=fingerprint)

        # SnapshotRepository
        self.snap_repo = AsyncMock()
        self.snap_repo.find_by_identity = AsyncMock(return_value=find_identity)
        self.snap_repo.create = AsyncMock(
            return_value=created_snapshot or _make_snapshot(),
        )

        # ArchitectAgent
        self.agent = AsyncMock()
        self.agent.run_with_metadata = AsyncMock(
            return_value=gen_result or _sample_gen_result(),
        )

        # MergeStep
        self.merge_instance = MagicMock()
        self.merge_cls = MagicMock(return_value=self.merge_instance)

        # MaterialState / MappingValidationState — used for enum comparison
        # These are imported inside the function via lazy import, so we
        # let them resolve naturally. Our mock entries use string values
        # which match the enum values.


def _make_session_factory(
    session: AsyncMock,
) -> MagicMock:
    """Create a mock async_sessionmaker that yields session via `async with`."""
    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=session)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = ctx_mgr
    return factory


async def _run_task(
    job_id: str,
    course_id: str,
    deps: _MockDeps,
    *,
    node_id: str | None = None,
    mode: str = "free",
) -> None:
    """Run arq_generate_structure with all dependencies patched."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    session_factory = _make_session_factory(session)

    ctx: dict[str, Any] = {
        "session_factory": session_factory,
        "model_router": AsyncMock(),
    }

    with (
        patch(
            "course_supporter.storage.job_repository.JobRepository",
            return_value=deps.job_repo,
        ),
        patch(
            "course_supporter.storage.material_node_repository.MaterialNodeRepository",
            return_value=deps.node_repo,
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
            "course_supporter.agents.architect.ArchitectAgent",
            return_value=deps.agent,
        ),
        patch(
            "course_supporter.ingestion.merge.MergeStep",
            deps.merge_cls,
        ),
    ):
        await arq_generate_structure(
            ctx,
            job_id,
            course_id,
            node_id=node_id,
            mode=mode,
        )


class TestHappyPathNodeLevel:
    """Node-level generation: READY materials → merge → agent → snapshot."""

    @pytest.mark.asyncio
    async def test_node_level_generates_snapshot(
        self,
        job_id: str,
        course_id: str,
        node_id_str: str,
    ) -> None:
        """Happy path: node-level generation creates snapshot and completes job."""
        nid = uuid.UUID(node_id_str)
        entry = _make_entry(state="ready")
        target = _make_node(node_id=nid, materials=[entry])
        root = _make_node(children=[target])

        snap = _make_snapshot()
        deps = _MockDeps(root_nodes=[root], created_snapshot=snap)

        await _run_task(job_id, course_id, deps, node_id=node_id_str)

        # Agent was called
        deps.agent.run_with_metadata.assert_called_once()
        # Snapshot was created
        deps.snap_repo.create.assert_called_once()
        # Job completed with snapshot id
        deps.job_repo.update_status.assert_any_call(
            uuid.UUID(job_id),
            "complete",
            result_snapshot_id=snap.id,
        )


class TestHappyPathCourseLevel:
    """Course-level generation: node_id=None → course fingerprint."""

    @pytest.mark.asyncio
    async def test_course_level_uses_course_fingerprint(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Course-level generation calls ensure_course_fp."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, course_id, deps, node_id=None)

        deps.fp_service.ensure_course_fp.assert_called_once_with([root])
        deps.fp_service.ensure_node_fp.assert_not_called()


class TestIdempotency:
    """Existing snapshot → agent NOT called → job complete with existing id."""

    @pytest.mark.asyncio
    async def test_idempotent_skips_agent(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Idempotency: existing snapshot skips LLM call."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        existing = _make_snapshot()

        deps = _MockDeps(root_nodes=[root], find_identity=existing)

        await _run_task(job_id, course_id, deps)

        deps.agent.run_with_metadata.assert_not_called()
        deps.snap_repo.create.assert_not_called()
        deps.job_repo.update_status.assert_any_call(
            uuid.UUID(job_id),
            "complete",
            result_snapshot_id=existing.id,
        )


class TestNoReadyMaterials:
    """No READY materials → job failed."""

    @pytest.mark.asyncio
    async def test_no_ready_materials_fails_job(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Task fails when no READY materials found."""
        raw_entry = _make_entry(state="raw")
        root = _make_node(materials=[raw_entry])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, course_id, deps)

        deps.agent.run_with_metadata.assert_not_called()


class TestAgentError:
    """ArchitectAgent error → job failed."""

    @pytest.mark.asyncio
    async def test_agent_error_fails_job(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Task fails when ArchitectAgent raises."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])

        deps = _MockDeps(root_nodes=[root])
        deps.agent.run_with_metadata.side_effect = RuntimeError("LLM boom")

        await _run_task(job_id, course_id, deps)

        deps.snap_repo.create.assert_not_called()


class TestMixedStates:
    """Only READY entries passed to merge, others ignored."""

    @pytest.mark.asyncio
    async def test_only_ready_entries_merged(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Only READY materials are collected for merge."""
        ready = _make_entry(state="ready")
        raw = _make_entry(state="raw")
        error = _make_entry(state="error")
        root = _make_node(materials=[ready, raw, error])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, course_id, deps)

        # MergeStep.merge called with 1 document (only ready)
        merge_call = deps.merge_instance.merge
        merge_call.assert_called_once()
        docs = merge_call.call_args[0][0]
        assert len(docs) == 1


class TestMappingsFiltering:
    """Validated mappings included, non-validated excluded."""

    @pytest.mark.asyncio
    async def test_validated_mappings_only(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Only validated mappings become SlideTimecodeRef."""
        entry = _make_entry(state="ready")
        valid = _make_mapping(validation_state="validated", slide_number=1)
        pending = _make_mapping(
            validation_state="pending_validation",
            slide_number=2,
        )
        failed = _make_mapping(
            validation_state="validation_failed",
            slide_number=3,
        )
        root = _make_node(materials=[entry], mappings=[valid, pending, failed])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, course_id, deps)

        merge_call = deps.merge_instance.merge
        merge_call.assert_called_once()
        mappings_arg = merge_call.call_args[0][1]
        assert len(mappings_arg) == 1
        assert mappings_arg[0].slide_number == 1


class TestLLMMetadata:
    """LLM metadata passed to snapshot create."""

    @pytest.mark.asyncio
    async def test_metadata_in_snapshot(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Snapshot receives model_id, tokens, cost from GenerationResult."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])

        gen_result = _sample_gen_result()
        deps = _MockDeps(root_nodes=[root], gen_result=gen_result)

        await _run_task(job_id, course_id, deps)

        create_kwargs = deps.snap_repo.create.call_args.kwargs
        assert create_kwargs["model_id"] == "gemini-2.5-flash"
        assert create_kwargs["tokens_in"] == 100
        assert create_kwargs["tokens_out"] == 200
        assert create_kwargs["cost_usd"] == 0.005
        assert create_kwargs["prompt_version"] == "v1"


class TestModePassthrough:
    """Mode=guided passed through pipeline."""

    @pytest.mark.asyncio
    async def test_guided_mode_in_snapshot(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Mode is passed to snapshot create and find_by_identity."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, course_id, deps, mode="guided")

        # find_by_identity called with mode="guided"
        identity_kwargs = deps.snap_repo.find_by_identity.call_args.kwargs
        assert identity_kwargs["mode"] == "guided"

        # create called with mode="guided"
        create_kwargs = deps.snap_repo.create.call_args.kwargs
        assert create_kwargs["mode"] == "guided"


class TestNodeNotFound:
    """Target node not found in tree → job failed."""

    @pytest.mark.asyncio
    async def test_node_not_found_fails(
        self,
        job_id: str,
        course_id: str,
    ) -> None:
        """Task fails when target node_id not found in tree."""
        root = _make_node()
        deps = _MockDeps(root_nodes=[root])

        missing_nid = str(uuid.uuid4())
        await _run_task(job_id, course_id, deps, node_id=missing_nid)

        deps.agent.run_with_metadata.assert_not_called()
