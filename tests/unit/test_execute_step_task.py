"""Tests for arq_execute_step background task (S3-020a)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.api.tasks import arq_execute_step
from course_supporter.models.course import CourseStructure, ModuleOutput
from course_supporter.models.step import StepOutput

# ── Helpers (reuse patterns from test_generate_structure_task) ──


def _make_node(
    *,
    node_id: uuid.UUID | None = None,
    parent_materialnode_id: uuid.UUID | None = None,
    title: str = "Test Node",
    description: str | None = None,
    order: int = 0,
    children: list[Any] | None = None,
    materials: list[Any] | None = None,
    mappings: list[Any] | None = None,
    node_fingerprint: str | None = None,
) -> MagicMock:
    """Create a mock MaterialNode."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.parent_materialnode_id = parent_materialnode_id
    node.title = title
    node.description = description
    node.order = order
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
    """Create a mock StructureSnapshot."""
    snap = MagicMock()
    snap.id = snapshot_id or uuid.uuid4()
    return snap


def _sample_step_output() -> StepOutput:
    """Create a sample StepOutput for mocking agent.execute()."""
    from course_supporter.llm.schemas import LLMResponse

    structure = CourseStructure(
        title="Generated Course",
        summary="A course about Python",
        modules=[ModuleOutput(title="Module 1")],
        core_concepts=["python"],
        mentioned_concepts=["java"],
    )
    response = LLMResponse(
        content="{}",
        provider="gemini",
        model_id="gemini-2.5-flash",
        tokens_in=100,
        tokens_out=200,
        cost_usd=0.005,
    )
    return StepOutput(
        structure=structure,
        summary="A course about Python",
        core_concepts=["python"],
        mentioned_concepts=["java"],
        prompt_version="v1_free",
        response=response,
    )


class _MockDeps:
    """Holds all mock dependencies for arq_execute_step."""

    def __init__(
        self,
        *,
        root_nodes: list[Any],
        find_identity: Any = None,
        step_output: StepOutput | None = None,
        created_snapshot: Any = None,
        fingerprint: str = "a" * 64,
    ) -> None:
        self.job_repo = AsyncMock()
        self.node_repo = AsyncMock()
        self.node_repo.get_subtree = AsyncMock(return_value=root_nodes)

        self.fp_service = AsyncMock()
        self.fp_service.ensure_node_fp = AsyncMock(return_value=fingerprint)
        self.fp_service.ensure_course_fp = AsyncMock(return_value=fingerprint)

        self.snap_repo = AsyncMock()
        self.snap_repo.find_by_identity = AsyncMock(return_value=find_identity)
        self.snap_repo.create = AsyncMock(
            return_value=created_snapshot or _make_snapshot(),
        )
        self.snap_repo.get_latest_for_nodes = AsyncMock(return_value={})

        self.agent = AsyncMock()
        self.agent.execute = AsyncMock(
            return_value=step_output or _sample_step_output(),
        )

        self.tree_summary: list[Any] = []


def _make_session_factory(session: AsyncMock) -> MagicMock:
    """Create a mock async_sessionmaker."""
    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=session)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = ctx_mgr
    return factory


async def _run_task(
    job_id: str,
    root_node_id: str,
    deps: _MockDeps,
    *,
    target_node_id: str | None = None,
    mode: str = "free",
    step_type: str = "generate",
) -> None:
    """Run arq_execute_step with all dependencies patched."""
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
            "course_supporter.tree_utils.build_material_tree_summary",
            return_value=deps.tree_summary,
        ),
    ):
        await arq_execute_step(
            ctx,
            job_id,
            root_node_id,
            target_node_id=target_node_id,
            mode=mode,
            step_type=step_type,
        )


@pytest.fixture()
def job_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def root_node_id() -> str:
    return str(uuid.uuid4())


class TestHappyPath:
    """arq_execute_step: READY materials → StepInput → agent.execute → snapshot."""

    async def test_creates_snapshot_with_step_fields(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Snapshot includes step_type, summary, core/mentioned_concepts."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        deps.agent.execute.assert_called_once()
        create_kwargs = deps.snap_repo.create.call_args.kwargs
        assert create_kwargs["step_type"] == "generate"
        assert create_kwargs["summary"] == "A course about Python"
        assert create_kwargs["core_concepts"] == ["python"]
        assert create_kwargs["mentioned_concepts"] == ["java"]

    async def test_job_completes(self, job_id: str, root_node_id: str) -> None:
        """Job transitions to complete on success."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        deps.job_repo.update_status.assert_any_call(uuid.UUID(job_id), "complete")

    async def test_esc_linked_to_snapshot(self, job_id: str, root_node_id: str) -> None:
        """ExternalServiceCall is created and linked to snapshot."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        create_kwargs = deps.snap_repo.create.call_args.kwargs
        assert "externalservicecall_id" in create_kwargs


class TestStepInputAssembly:
    """StepInput is built correctly from tree data."""

    async def test_passes_step_input_to_agent(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Agent receives a StepInput with correct fields."""
        from course_supporter.models.step import StepInput

        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        call_args = deps.agent.execute.call_args
        step_input = call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert step_input.mode == "free"
        assert step_input.existing_structure is None
        assert step_input.children_summaries == []
        assert step_input.parent_context is None

    async def test_guided_mode_passes_existing_structure(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Guided mode injects existing_structure into StepInput."""
        from course_supporter.models.step import StepInput

        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry], title="My Module")
        root.parent_materialnode_id = None
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps, mode="guided")

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert step_input.existing_structure is not None
        assert "My Module" in step_input.existing_structure

    async def test_mappings_in_step_input(self, job_id: str, root_node_id: str) -> None:
        """Validated mappings are included as slide_timecode_refs."""
        entry = _make_entry(state="ready")
        valid = _make_mapping(validation_state="validated", slide_number=1)
        pending = _make_mapping(validation_state="pending_validation", slide_number=2)
        root = _make_node(materials=[entry], mappings=[valid, pending])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        assert len(step_input.slide_timecode_refs) == 1
        assert step_input.slide_timecode_refs[0].slide_number == 1


class TestChildrenSummaries:
    """Children summaries loaded from latest snapshots of child nodes."""

    async def test_children_summaries_passed_to_agent(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Parent node receives children summaries in StepInput."""
        from course_supporter.models.step import StepInput

        child = _make_node(title="Child Topic")
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry], children=[child])

        child_snap = MagicMock()
        child_snap.id = uuid.uuid4()
        child_snap.materialnode_id = child.id
        child_snap.summary = "Child covers basics"
        child_snap.core_concepts = ["variables"]
        child_snap.mentioned_concepts = ["functions"]

        deps = _MockDeps(root_nodes=[root])
        deps.snap_repo.get_latest_for_nodes = AsyncMock(
            return_value={child.id: child_snap},
        )

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert len(step_input.children_summaries) == 1
        summary = step_input.children_summaries[0]
        assert summary.node_id == child.id
        assert summary.title == "Child Topic"
        assert summary.summary == "Child covers basics"
        assert summary.core_concepts == ["variables"]

    async def test_children_without_snapshots_skipped(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Children without snapshots are excluded from summaries."""
        child = _make_node(title="No Snapshot Child")
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry], children=[child])

        deps = _MockDeps(root_nodes=[root])
        # get_latest_for_nodes returns empty dict (default)

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.children_summaries == []


class TestIdempotency:
    """Existing snapshot → agent NOT called."""

    async def test_idempotent_skips_agent(self, job_id: str, root_node_id: str) -> None:
        """Existing fingerprint match skips LLM call."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        existing = _make_snapshot()
        deps = _MockDeps(root_nodes=[root], find_identity=existing)

        await _run_task(job_id, root_node_id, deps)

        deps.agent.execute.assert_not_called()
        deps.snap_repo.create.assert_not_called()
        deps.job_repo.update_status.assert_any_call(uuid.UUID(job_id), "complete")


class TestErrorHandling:
    """Agent error → job failed with cascading."""

    async def test_agent_error_fails_job(self, job_id: str, root_node_id: str) -> None:
        """Agent exception triggers failure path."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])
        deps.agent.execute.side_effect = RuntimeError("LLM boom")

        await _run_task(job_id, root_node_id, deps)

        deps.snap_repo.create.assert_not_called()

    async def test_no_ready_materials_fails(
        self, job_id: str, root_node_id: str
    ) -> None:
        """No READY materials triggers failure."""
        raw_entry = _make_entry(state="raw")
        root = _make_node(materials=[raw_entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        deps.agent.execute.assert_not_called()


class TestCorrectionsSerialize:
    """Corrections from StepOutput serialize to JSONB dict."""

    async def test_corrections_serialized(self, job_id: str, root_node_id: str) -> None:
        """StepOutput corrections become list-of-dicts in snapshot."""
        from course_supporter.llm.schemas import LLMResponse
        from course_supporter.models.step import Correction, CorrectionAction

        target_nid = uuid.uuid4()
        output = StepOutput(
            structure=CourseStructure(title="T", summary="S"),
            summary="S",
            core_concepts=[],
            mentioned_concepts=[],
            prompt_version="v1",
            response=LLMResponse(
                content="{}",
                provider="test",
                model_id="test",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
            ),
            corrections=[
                Correction(
                    target_node_id=target_nid,
                    field="title",
                    action=CorrectionAction.RENAME,
                    old_value="old",
                    new_value="new",
                    reason="consistency",
                )
            ],
            terminology_map={"var": "variable"},
        )

        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root], step_output=output)

        await _run_task(job_id, root_node_id, deps)

        create_kwargs = deps.snap_repo.create.call_args.kwargs
        corrections = create_kwargs["corrections"]
        assert len(corrections) == 1
        assert corrections[0]["target_node_id"] == str(target_nid)
        assert corrections[0]["action"] == "rename"
