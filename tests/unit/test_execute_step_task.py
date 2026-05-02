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
    parent_id: uuid.UUID | None = None,
    parent: MagicMock | None = None,
    title: str = "Test Node",
    description: str | None = None,
    order: int = 0,
    children: list[Any] | None = None,
    materials: list[Any] | None = None,
    content_hash: str | None = None,
) -> MagicMock:
    """Create a mock CourseNode."""
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.parent_id = parent_id
    node.parent = parent
    node.title = title
    node.description = description
    node.order = order
    node.children = children or []
    node.materials = materials or []
    node.content_hash = content_hash
    return node


def _make_entry(
    *,
    state: str = "ready",
    processed_content: str | None = None,
    outline_content: str | None = None,
) -> MagicMock:
    """Create a mock AuthoredDocument."""
    entry = MagicMock()
    entry.state = state
    entry.processed_content = processed_content or (
        '{"source_type": "text", "source_url": "file:///test.md"}'
    )
    entry.outline_content = outline_content
    return entry


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
        self.snap_repo.get_latest_for_node = AsyncMock(return_value=None)

        self.agent = AsyncMock()
        self.agent.execute = AsyncMock(
            return_value=step_output or _sample_step_output(),
        )

        self.tree_summary: list[Any] = []

        # EditableRepository — return value not consumed by caller
        self.editable_repo = AsyncMock()
        self.editable_repo.init_from_snapshot = AsyncMock(return_value=[])


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
            "course_supporter.storage.course_node_repository.CourseNodeRepository",
            return_value=deps.node_repo,
        ),
        patch(
            "course_supporter.fingerprint.FingerprintService",
            return_value=deps.fp_service,
        ),
        patch(
            "course_supporter.api.tasks.SnapshotRepository",
            return_value=deps.snap_repo,
        ),
        patch(
            "course_supporter.api.tasks.ArchitectAgent",
            return_value=deps.agent,
        ),
        patch(
            "course_supporter.api.tasks.ReconcileAgent",
            return_value=deps.agent,
        ),
        patch(
            "course_supporter.api.tasks.RefineAgent",
            return_value=deps.agent,
        ),
        patch(
            "course_supporter.tree_utils.build_material_tree_summary",
            return_value=deps.tree_summary,
        ),
        patch(
            "course_supporter.api.tasks.EditableRepository",
            return_value=deps.editable_repo,
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
        deps.editable_repo.init_from_snapshot.assert_called_once()
        call_kwargs = deps.editable_repo.init_from_snapshot.call_args.kwargs
        assert call_kwargs["preserve_edited"] is True

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
        root.parent_id = None
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps, mode="guided")

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert step_input.existing_structure is not None
        assert "My Module" in step_input.existing_structure


class TestChildrenSummaries:
    """Children summaries loaded from latest snapshots of child nodes."""

    async def test_children_summaries_passed_to_agent(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Parent node receives children summaries when no full snapshots exist."""
        from course_supporter.models.step import StepInput

        child = _make_node(title="Child Topic")
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry], children=[child])

        # Snapshot with summary but NO structure → not a full snapshot,
        # so children_summaries path is used instead of children_snapshots.
        child_snap = MagicMock()
        child_snap.id = uuid.uuid4()
        child_snap.course_node_id = child.id
        child_snap.summary = "Child covers basics"
        child_snap.core_concepts = ["variables"]
        child_snap.mentioned_concepts = ["functions"]
        child_snap.structure = None  # no full structure → not a snapshot

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


def _make_snap_with_summary(
    *,
    node_id: uuid.UUID,
    summary: str = "Summary",
    core_concepts: list[str] | None = None,
    mentioned_concepts: list[str] | None = None,
) -> MagicMock:
    """Create a mock StructureSnapshot with summary fields."""
    snap = MagicMock()
    snap.id = uuid.uuid4()
    snap.course_node_id = node_id
    snap.summary = summary
    snap.core_concepts = core_concepts or []
    snap.mentioned_concepts = mentioned_concepts or []
    return snap


class TestReconcileSlidingWindow:
    """Reconcile steps load parent context and sibling summaries."""

    async def test_parent_context_loaded_for_reconcile(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Reconcile step receives parent_context in StepInput."""
        from course_supporter.models.step import StepInput

        parent_id = uuid.UUID(root_node_id)
        entry = _make_entry(state="ready")
        child = _make_node(
            title="Child",
            materials=[entry],
            parent_id=parent_id,
        )
        parent = _make_node(
            node_id=parent_id,
            title="Parent",
            children=[child],
        )
        child.parent = parent

        parent_snap = _make_snap_with_summary(
            node_id=parent_id,
            summary="Parent summary",
            core_concepts=["overview"],
        )

        deps = _MockDeps(root_nodes=[parent])
        # get_latest_for_node returns parent snap
        deps.snap_repo.get_latest_for_node = AsyncMock(return_value=parent_snap)

        await _run_task(
            job_id,
            root_node_id,
            deps,
            target_node_id=str(child.id),
            step_type="reconcile",
        )

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert step_input.parent_context is not None
        assert step_input.parent_context.node_id == parent_id
        assert step_input.parent_context.summary == "Parent summary"
        assert step_input.parent_context.core_concepts == ["overview"]

    async def test_sibling_summaries_loaded_for_reconcile(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Reconcile step receives sibling_summaries in StepInput."""
        from course_supporter.models.step import StepInput

        parent_id = uuid.UUID(root_node_id)
        entry = _make_entry(state="ready")
        target_child = _make_node(
            title="Target",
            materials=[entry],
            parent_id=parent_id,
        )
        sibling = _make_node(
            title="Sibling",
            parent_id=parent_id,
        )
        parent = _make_node(
            node_id=parent_id,
            title="Parent",
            children=[target_child, sibling],
        )
        target_child.parent = parent
        sibling.parent = parent

        sibling_snap = _make_snap_with_summary(
            node_id=sibling.id,
            summary="Sibling covers arrays",
            core_concepts=["arrays"],
        )

        deps = _MockDeps(root_nodes=[parent])
        # get_latest_for_nodes returns sibling snap
        deps.snap_repo.get_latest_for_nodes = AsyncMock(
            return_value={sibling.id: sibling_snap},
        )

        await _run_task(
            job_id,
            root_node_id,
            deps,
            target_node_id=str(target_child.id),
            step_type="reconcile",
        )

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        assert len(step_input.sibling_summaries) == 1
        assert step_input.sibling_summaries[0].node_id == sibling.id
        assert step_input.sibling_summaries[0].summary == "Sibling covers arrays"

    async def test_generate_step_skips_parent_and_siblings(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Generate steps do NOT load parent/sibling context."""
        parent_id = uuid.UUID(root_node_id)
        entry = _make_entry(state="ready")
        child = _make_node(
            title="Child",
            materials=[entry],
            parent_id=parent_id,
        )
        parent = _make_node(
            node_id=parent_id,
            title="Parent",
            children=[child],
        )
        child.parent = parent

        deps = _MockDeps(root_nodes=[parent])

        await _run_task(
            job_id,
            root_node_id,
            deps,
            target_node_id=str(child.id),
            step_type="generate",
        )

        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.parent_context is None
        assert step_input.sibling_summaries == []
        # get_latest_for_node should NOT have been called for parent
        deps.snap_repo.get_latest_for_node.assert_not_called()

    async def test_root_reconcile_no_parent_context(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Root node reconcile: parent_context is None, siblings empty."""
        entry = _make_entry(state="ready")
        child = _make_node(title="Child")
        root = _make_node(
            materials=[entry],
            children=[child],
        )
        # Root has no parent
        root.parent = None
        root.parent_id = None

        deps = _MockDeps(root_nodes=[root])

        await _run_task(
            job_id,
            root_node_id,
            deps,
            step_type="reconcile",
        )

        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.parent_context is None
        assert step_input.sibling_summaries == []


class TestContextCompression:
    """Parent nodes with child snapshots use only own materials."""

    async def test_parent_with_child_snapshots_uses_own_materials(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Parent node collects only its own materials when children have snapshots."""
        from course_supporter.models.step import StepInput

        parent_entry = _make_entry(
            state="ready",
            processed_content='{"source_type": "text", "source_url": "file:///parent.md"}',
        )
        child_entry = _make_entry(
            state="ready",
            processed_content='{"source_type": "text", "source_url": "file:///child.md"}',
        )
        child = _make_node(title="Child Topic", materials=[child_entry])
        root = _make_node(materials=[parent_entry], children=[child])

        child_snap = MagicMock()
        child_snap.id = uuid.uuid4()
        child_snap.course_node_id = child.id
        child_snap.structure = {"modules": [{"title": "Sub"}]}
        child_snap.summary = "Child covers basics"
        child_snap.core_concepts = ["variables"]
        child_snap.mentioned_concepts = ["functions"]
        child_snap.summary_nested_nodes = "Nested info"

        deps = _MockDeps(root_nodes=[root])
        deps.snap_repo.get_latest_for_nodes = AsyncMock(
            return_value={child.id: child_snap},
        )

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        assert isinstance(step_input, StepInput)
        # Should have children_snapshots
        assert len(step_input.children_snapshots) == 1
        assert step_input.children_snapshots[0].title == "Child Topic"
        assert step_input.children_snapshots[0].summary_nested_nodes == "Nested info"
        # Only parent's own materials, not child's
        assert len(step_input.materials) == 1
        assert step_input.materials[0].source_url == "file:///parent.md"
        # children_summaries skipped when full snapshots are available
        assert step_input.children_summaries == []

    async def test_parent_without_own_materials_still_works(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Parent with no own materials but children with snapshots → no error."""
        child_entry = _make_entry(state="ready")
        child = _make_node(title="Child", materials=[child_entry])
        # Parent has no materials
        root = _make_node(materials=[], children=[child])

        child_snap = MagicMock()
        child_snap.id = uuid.uuid4()
        child_snap.course_node_id = child.id
        child_snap.structure = {"modules": []}
        child_snap.summary = "Child summary"
        child_snap.core_concepts = []
        child_snap.mentioned_concepts = []
        child_snap.summary_nested_nodes = ""

        deps = _MockDeps(root_nodes=[root])
        deps.snap_repo.get_latest_for_nodes = AsyncMock(
            return_value={child.id: child_snap},
        )

        await _run_task(job_id, root_node_id, deps)

        # Agent was called (no NoReadyMaterialsError)
        deps.agent.execute.assert_called_once()
        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.materials == []

    async def test_leaf_node_uses_all_materials(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Leaf node without children uses all subtree materials (unchanged)."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])

        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        assert len(step_input.materials) == 1
        assert step_input.children_snapshots == []

    async def test_children_without_snapshots_uses_subtree(
        self, job_id: str, root_node_id: str
    ) -> None:
        """Children without snapshots → fallback to subtree materials."""
        child_entry = _make_entry(state="ready")
        child = _make_node(title="Child", materials=[child_entry])
        root = _make_node(materials=[], children=[child])

        deps = _MockDeps(root_nodes=[root])
        # get_latest_for_nodes returns empty dict (no snapshots)

        await _run_task(job_id, root_node_id, deps)

        step_input = deps.agent.execute.call_args[0][0]
        # Should have used full subtree (child's materials)
        assert len(step_input.materials) == 1
        assert step_input.children_snapshots == []


class TestAgentDispatch:
    """Step Executor dispatches to correct agent based on step_type."""

    async def test_reconcile_calls_agent_execute(
        self, job_id: str, root_node_id: str
    ) -> None:
        """step_type='reconcile' still calls agent.execute()."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps, step_type="reconcile")

        deps.agent.execute.assert_called_once()
        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.step_type.value == "reconcile"

    async def test_generate_calls_agent_execute(
        self, job_id: str, root_node_id: str
    ) -> None:
        """step_type='generate' calls agent.execute()."""
        entry = _make_entry(state="ready")
        root = _make_node(materials=[entry])
        deps = _MockDeps(root_nodes=[root])

        await _run_task(job_id, root_node_id, deps, step_type="generate")

        deps.agent.execute.assert_called_once()
        step_input = deps.agent.execute.call_args[0][0]
        assert step_input.step_type.value == "generate"


class TestDetermineNodePosition:
    """Unit tests for _determine_node_position helper."""

    def test_root_node(self) -> None:
        from course_supporter.agents.architect import NodePosition
        from course_supporter.api.tasks import _determine_node_position

        node = _make_node(parent_id=None, children=[])
        assert _determine_node_position(node) == NodePosition.ROOT

    def test_leaf_node(self) -> None:
        from course_supporter.agents.architect import NodePosition
        from course_supporter.api.tasks import _determine_node_position

        node = _make_node(parent_id=uuid.uuid4(), children=[])
        assert _determine_node_position(node) == NodePosition.LEAF

    def test_intermediate_node(self) -> None:
        from course_supporter.agents.architect import NodePosition
        from course_supporter.api.tasks import _determine_node_position

        child = _make_node()
        node = _make_node(parent_id=uuid.uuid4(), children=[child])
        assert _determine_node_position(node) == NodePosition.INTERMEDIATE
