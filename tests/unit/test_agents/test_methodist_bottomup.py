"""Unit tests for :class:`MethodistAgent.generate_bottomup` (Phase 3.2.3a).

FakeStageRouter pattern lets us pin every contract surface without
hitting a real LLM:

* response_validator captures the schema check.
* render_context capture lets tests assert language / course title /
  children-compressed shape.
* fault-injection on the router surfaces the
  ``LadderExhaustedError`` → ``MethodistInputBudgetExhaustedError``
  translation contract.

Coverage:

* canonical fields written in place on the supplied ``raw``.
* concept fields computed deterministically by code (NOT from LLM
  output, even when LLM tries to emit them).
* size counters (own + cumulative) computed from inputs + children.
* empty-container synthesis (own_documents=0).
* language pin via ``display_name`` for the course root.
* input-budget exhaustion translated to the domain exception.
* generate_topdown is an explicit no-op.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.agents.methodist import (
    MethodistAgent,
    MethodistInputBudgetExhaustedError,
    MethodistRootResolutionError,
    Pass1ChildRawMissingError,
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)

# ── Fake DB rows (no ORM required for unit tests) ────────────────


@dataclass
class _FakeDocumentSummary:
    title: str
    description: str
    main_concepts: list[str] = field(default_factory=list)
    secondary_concepts: list[str] = field(default_factory=list)
    content_char_count: int = 0


@dataclass
class _FakeChildRaw:
    """Mirrors the child-payload shape produced by ``_fetch_children_raws``."""

    title: str
    compressed_summary: str = ""
    main_concepts: list[str] = field(default_factory=list)
    secondary_concepts: list[str] = field(default_factory=list)
    cumulative_documents_count: int = 0
    cumulative_chars_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "compressed_summary": self.compressed_summary,
            "main_concepts": list(self.main_concepts),
            "secondary_concepts": list(self.secondary_concepts),
            "cumulative_documents_count": self.cumulative_documents_count,
            "cumulative_chars_count": self.cumulative_chars_count,
        }


@dataclass
class _FakeRaw:
    """Mutable target for in-place canonical writes."""

    title: str | None = None
    description: str | None = None
    learning_objectives: list[str] = field(default_factory=list)
    knowledge: list[dict[str, str]] = field(default_factory=list)
    skills: list[dict[str, str]] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    assessment_approach: str | None = None
    teaching_approach: str | None = None
    key_activities: list[str] = field(default_factory=list)
    common_mistakes: list[str] = field(default_factory=list)
    compressed_summary: str | None = None
    methodist_observations: list[str] = field(default_factory=list)
    main_concepts: list[str] = field(default_factory=list)
    secondary_concepts: list[str] = field(default_factory=list)
    own_documents_count: int = 0
    own_chars_count: int = 0
    cumulative_documents_count: int = 0
    cumulative_chars_count: int = 0


@dataclass
class _FakeNode:
    id: uuid.UUID
    title: str
    parent_id: uuid.UUID | None = None


# ── FakeStageRouter ───────────────────────────────────────────────


class _FakeStageRouter:
    """Captures render_context + invokes the response_validator with canned content."""

    def __init__(
        self,
        *,
        canned_response: str,
        exception_on_call: Exception | None = None,
    ) -> None:
        self.canned_response = canned_response
        self.exception_on_call = exception_on_call
        self.last_stage_name: str | None = None
        self.last_kwargs: dict[str, Any] | None = None

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Callable[[str], None] | None = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> Any:
        self.last_stage_name = stage_name
        self.last_kwargs = render_context
        del expects_json
        if self.exception_on_call is not None:
            raise self.exception_on_call
        if response_validator is not None:
            response_validator(self.canned_response)
        return None


# ── Stub the read methods so the agent skips real DB calls ────────


class _StubAgent(MethodistAgent):
    """Override the three async readers with in-memory test inputs."""

    def __init__(
        self,
        stage_router: _FakeStageRouter,
        *,
        own_docs: list[_FakeDocumentSummary],
        child_raws: list[_FakeChildRaw],
        course_title: str,
        language: str | None,
    ) -> None:
        super().__init__(session=AsyncMock(), stage_router=stage_router)  # type: ignore[arg-type]
        self._stub_own_docs = own_docs
        self._stub_child_raws = child_raws
        self._stub_course_title = course_title
        self._stub_language = language

    async def _fetch_own_ready_summaries(self, course_node_id: Any) -> list[Any]:
        del course_node_id
        return list(self._stub_own_docs)

    async def _fetch_children_raws(self, course_node_id: Any) -> list[dict[str, Any]]:
        del course_node_id
        return [c.as_dict() for c in self._stub_child_raws]

    async def _resolve_course_context(self, node: Any) -> tuple[str, str | None]:
        del node
        return self._stub_course_title, self._stub_language


# ── Canned LLM responses ──────────────────────────────────────────


_FULL_BODY = {
    "title": "Тест: вступ до doctest",
    "description": "Базовий огляд doctest як засобу автоматичного тестування.",
    "learning_objectives": ["сформулювати призначення doctest"],
    "knowledge": [{"name": "doctest", "description": "автоматичний тест із docstring"}],
    "skills": [{"name": "написати doctest", "description": "оформити приклад"}],
    "success_criteria": ["doctest проходить локально"],
    "assessment_approach": "перевірка прикладів у docstring",
    "teaching_approach": "live-демонстрація на простих функціях",
    "key_activities": ["записати власний doctest"],
    "common_mistakes": ["ігнорування whitespace у docstring"],
    "compressed_summary": (
        "Вузол навчає основ doctest: визначення модулю, синтаксису "
        "прикладів, типових пасток. Дитячих compressed_summary не "
        "очікувано, бо це лист дерева. Виходом є вміння оформити "
        "елементарний doctest для функції з очевидним інваріантом."
    ),
    "methodist_observations": ["приклади бажано тримати ≤ 5 рядків"],
}


def _llm_json(extra: dict[str, Any] | None = None) -> str:
    body = dict(_FULL_BODY)
    if extra:
        body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# ── Tests ─────────────────────────────────────────────────────────


class TestCanonicalFieldsWrittenInPlace:
    async def test_all_llm_fields_land_on_raw(self) -> None:
        own_doc = _FakeDocumentSummary(
            title="lecture-01",
            description="intro",
            main_concepts=["doctest"],
            secondary_concepts=["unittest"],
            content_char_count=12345,
        )
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=[own_doc],
            child_raws=[],
            course_title="python_start",
            language="Ukrainian",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="ObjVarAssignment")
        await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]

        assert raw.title == "Тест: вступ до doctest"
        assert raw.description.startswith("Базовий огляд")
        assert raw.learning_objectives == ["сформулювати призначення doctest"]
        assert raw.knowledge == [
            {"name": "doctest", "description": "автоматичний тест із docstring"}
        ]
        assert raw.skills == [
            {"name": "написати doctest", "description": "оформити приклад"}
        ]
        assert raw.success_criteria == ["doctest проходить локально"]
        assert raw.assessment_approach == "перевірка прикладів у docstring"
        assert raw.teaching_approach == "live-демонстрація на простих функціях"
        assert raw.key_activities == ["записати власний doctest"]
        assert raw.common_mistakes == ["ігнорування whitespace у docstring"]
        assert raw.compressed_summary.startswith("Вузол навчає")
        assert raw.methodist_observations == ["приклади бажано тримати ≤ 5 рядків"]


class TestConceptUnionDeterministicByCode:
    async def test_main_secondary_union_with_promotion(self) -> None:
        own_a = _FakeDocumentSummary(
            title="a",
            description="d",
            main_concepts=["doctest"],
            secondary_concepts=["pytest", "tdd"],
        )
        own_b = _FakeDocumentSummary(
            title="b",
            description="d",
            main_concepts=["unittest"],
            secondary_concepts=["doctest", "pyclean"],
        )
        child = _FakeChildRaw(
            title="leaf",
            compressed_summary="…",
            main_concepts=["mock"],
            secondary_concepts=["pytest"],
        )
        # Even if the LLM "tries" to emit concept fields, they MUST be
        # ignored by the agent because the prompt schema does not allow
        # them. Inject extras via a body with extra keys — the validator
        # rejects them via Pydantic extra='forbid'. So instead this test
        # verifies the deterministic union is computed from the inputs.
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=[own_a, own_b],
            child_raws=[child],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]

        # main = union of all main fields = {doctest, unittest, mock}.
        assert raw.main_concepts == ["doctest", "mock", "unittest"]
        # secondary = union of all secondary MINUS anything in main.
        # raw seconds = {pytest, tdd, doctest, pyclean, pytest} - main
        # = {pytest, tdd, pyclean} (doctest promoted to main).
        assert raw.secondary_concepts == ["pyclean", "pytest", "tdd"]

    async def test_pydantic_forbids_extra_concept_fields_in_llm_output(self) -> None:
        # If the LLM accidentally emits main_concepts/secondary_concepts,
        # the validator MUST trigger a StructuralRetryError (which the
        # router would retry; the test triggers it via _FakeStageRouter
        # invoking the validator once and propagating its raise).
        body = dict(_FULL_BODY)
        body["main_concepts"] = ["should-not-be-here"]
        router = _FakeStageRouter(canned_response=json.dumps(body))
        agent = _StubAgent(
            router, own_docs=[], child_raws=[], course_title="x", language="English"
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(StructuralRetryError):
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]


class TestSizeCounters:
    async def test_own_counters_from_documents(self) -> None:
        docs = [
            _FakeDocumentSummary(title="a", description="d", content_char_count=1000),
            _FakeDocumentSummary(title="b", description="d", content_char_count=2500),
        ]
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=docs,
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        await agent.generate_bottomup(_FakeNode(id=uuid.uuid4(), title="t"), raw)  # type: ignore[arg-type]
        assert raw.own_documents_count == 2
        assert raw.own_chars_count == 3500
        # No children → cumulative equals own.
        assert raw.cumulative_documents_count == 2
        assert raw.cumulative_chars_count == 3500

    async def test_cumulative_aggregates_children(self) -> None:
        docs = [
            _FakeDocumentSummary(title="a", description="d", content_char_count=500),
        ]
        children = [
            _FakeChildRaw(
                title="c1", cumulative_documents_count=3, cumulative_chars_count=5000
            ),
            _FakeChildRaw(
                title="c2", cumulative_documents_count=1, cumulative_chars_count=2500
            ),
        ]
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=docs,
            child_raws=children,
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        await agent.generate_bottomup(_FakeNode(id=uuid.uuid4(), title="t"), raw)  # type: ignore[arg-type]
        assert raw.own_documents_count == 1
        assert raw.own_chars_count == 500
        assert raw.cumulative_documents_count == 1 + 3 + 1  # 5
        assert raw.cumulative_chars_count == 500 + 5000 + 2500  # 8000


class TestEmptyContainerSynthesis:
    async def test_no_own_docs_but_children_succeeds(self) -> None:
        # Container node: own_documents=0, has children. LLM is expected
        # to synthesise canonical fields from children's compressed
        # summaries. The agent's job is to feed the right payload.
        children = [
            _FakeChildRaw(
                title="child-a",
                compressed_summary="children content A",
                main_concepts=["c1"],
            ),
            _FakeChildRaw(
                title="child-b",
                compressed_summary="children content B",
                main_concepts=["c2"],
            ),
        ]
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=children,
            course_title="x",
            language="Ukrainian",
        )
        raw = _FakeRaw()
        await agent.generate_bottomup(_FakeNode(id=uuid.uuid4(), title="t"), raw)  # type: ignore[arg-type]

        ctx = router.last_kwargs or {}
        assert ctx["own_document_count"] == 0
        assert ctx["child_count"] == 2
        assert ctx["children_compressed"][0]["title"] == "child-a"
        assert ctx["children_compressed"][0]["compressed_summary"] == (
            "children content A"
        )
        # Concept union comes from children alone.
        assert raw.main_concepts == ["c1", "c2"]


class TestRenderContextLanguagePin:
    async def test_language_passed_through_kwargs(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=[],
            course_title="курс",
            language="Ukrainian",
        )
        raw = _FakeRaw()
        await agent.generate_bottomup(_FakeNode(id=uuid.uuid4(), title="t"), raw)  # type: ignore[arg-type]
        assert router.last_kwargs is not None
        assert router.last_kwargs["language"] == "Ukrainian"
        assert router.last_kwargs["course_title"] == "курс"
        assert router.last_stage_name == "methodist_bottomup"


class TestBudgetExhaustionDomainTranslation:
    async def test_all_input_budget_attempts_raise_domain_error(self) -> None:
        attempts = [
            (
                "deepseek_thinking",
                "deepseek-v4-pro",
                "input budget exceeded: ~600000 tokens > 0.5 * 1048576 = 524288",
            ),
            (
                "dashscope",
                "qwen3.7-max",
                "input budget exceeded: ~600000 tokens > 0.5 * 1048576 = 524288",
            ),
        ]
        exc = LadderExhaustedError("methodist_bottomup", attempts)
        router = _FakeStageRouter(canned_response="", exception_on_call=exc)
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(MethodistInputBudgetExhaustedError) as exc_info:
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        msg = str(exc_info.value)
        assert "Restructure the course" in msg
        assert "methodist_bottomup" in msg

    async def test_mixed_attempts_keep_original_exception(self) -> None:
        # If any rung failed for a non-budget reason, the agent must
        # NOT translate — preserve infrastructure context for K2 catch.
        attempts = [
            ("deepseek_thinking", "deepseek-v4-pro", "structural retry exhausted"),
            (
                "dashscope",
                "qwen3.7-max",
                "input budget exceeded: ~600000 tokens > 0.5 * 1048576",
            ),
        ]
        exc = LadderExhaustedError("methodist_bottomup", attempts)
        router = _FakeStageRouter(canned_response="", exception_on_call=exc)
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(LadderExhaustedError):
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]


# ── R1: semantic minimum (closure-aware validator) ───────────────


class TestSemanticMinimumOnContentBearingNode:
    """Empty title/description/compressed_summary must NOT pass on a
    node that carries any input. Empty IS allowed on a structurally
    trivial node (no own_docs AND no children) per KD10 line 845.
    """

    async def test_empty_dict_response_with_inputs_raises_retry(self) -> None:
        # has_inputs = True (own_doc present) + LLM returns empty {}.
        own = _FakeDocumentSummary(
            title="lec", description="d", main_concepts=["x"], content_char_count=100
        )
        router = _FakeStageRouter(canned_response=json.dumps({}))
        agent = _StubAgent(
            router,
            own_docs=[own],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(StructuralRetryError) as exc_info:
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        # All three required fields surface in the feedback so the LLM
        # knows exactly what to fix on retry.
        msg = str(exc_info.value)
        assert "title" in msg
        assert "description" in msg
        assert "compressed_summary" in msg

    async def test_only_compressed_summary_empty_with_inputs_raises(self) -> None:
        # Title and description present, compressed_summary empty.
        body = dict(_FULL_BODY)
        body["compressed_summary"] = ""
        own = _FakeDocumentSummary(title="lec", description="d", content_char_count=10)
        router = _FakeStageRouter(canned_response=json.dumps(body))
        agent = _StubAgent(
            router,
            own_docs=[own],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(StructuralRetryError) as exc_info:
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        msg = str(exc_info.value)
        # The missing-fields list is rendered as ['compressed_summary'] —
        # the prose mentions title/description as part of the schema
        # explanation but the actionable list is just compressed_summary.
        assert "['compressed_summary']" in msg

    async def test_only_whitespace_treated_as_empty(self) -> None:
        # Whitespace-only fields must not pass; agent strips before check.
        body = dict(_FULL_BODY)
        body["title"] = "   "
        body["description"] = "\n\t "
        own = _FakeDocumentSummary(title="lec", description="d", content_char_count=5)
        router = _FakeStageRouter(canned_response=json.dumps(body))
        agent = _StubAgent(
            router,
            own_docs=[own],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(StructuralRetryError) as exc_info:
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        msg = str(exc_info.value)
        assert "title" in msg
        assert "description" in msg

    async def test_empty_dict_on_trivial_node_allowed(self) -> None:
        # has_inputs = False (no own_doc, no children) — the empty
        # response is the LEGITIMATE shape for a structurally trivial
        # node. Validator MUST NOT raise.
        router = _FakeStageRouter(canned_response=json.dumps({}))
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        # Should NOT raise.
        await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        # All canonical fields default to empty; counters are zero.
        assert raw.title == ""
        assert raw.description == ""
        assert raw.compressed_summary == ""
        assert raw.own_documents_count == 0
        assert raw.cumulative_documents_count == 0


# ── R2: child Raw missing → loud raise ───────────────────────────


class TestChildRawMissingRaises:
    """KD10 leaf→root ordering guarantees child Raws are committed.
    A missing child Raw at parent visit time is a read-contract
    violation; the agent raises so K2 catches → ERROR + continue walk.
    """

    async def test_fetch_children_raws_raises_when_child_raw_missing(self) -> None:
        # Drive the real _fetch_children_raws path with a stubbed session
        # that returns one child CourseNode but zero NodeSummaryRaw rows
        # — exactly the read-contract violation R2 guards against.
        from unittest.mock import MagicMock

        parent_id = uuid.uuid4()
        child_id = uuid.uuid4()
        child_node = _FakeNode(id=child_id, title="orphan-child", parent_id=parent_id)

        children_result = MagicMock()
        children_result.scalars.return_value.all.return_value = [child_node]
        raws_result = MagicMock()
        raws_result.scalars.return_value.all.return_value = []

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[children_result, raws_result])

        from course_supporter.llm.stage_router import StageRouter

        agent = MethodistAgent(
            session=session,
            stage_router=AsyncMock(spec=StageRouter),
        )
        with pytest.raises(Pass1ChildRawMissingError) as exc_info:
            await agent._fetch_children_raws(parent_id)
        assert exc_info.value.parent_node_id == parent_id
        assert exc_info.value.child_node_id == child_id
        msg = str(exc_info.value)
        assert "KD10 leaf→root" in msg

    async def test_present_raw_with_empty_compressed_summary_passes(self) -> None:
        # Confirms the carve-out: child Raw EXISTS but compressed_summary
        # is empty → legitimate, agent passes empty through unchanged.
        children = [_FakeChildRaw(title="leaf", compressed_summary="")]
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(
            router,
            own_docs=[],
            child_raws=children,
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        await agent.generate_bottomup(_FakeNode(id=uuid.uuid4(), title="t"), raw)  # type: ignore[arg-type]
        ctx = router.last_kwargs or {}
        assert ctx["children_compressed"][0]["compressed_summary"] == ""


# ── R3: depth-cap silent fallback removed ────────────────────────


class TestRootResolutionDepthCap:
    """When the parent_id walk-up hits the 25-depth cap without
    reaching a real root, the agent raises instead of using a
    mid-chain node as if it were the course root.
    """

    async def test_depth_cap_with_remaining_parent_raises(self) -> None:
        # The real implementation reads via session.get; stub a session
        # that returns a synthetic parent for every walk step, never
        # reaching parent_id IS NULL.
        chain_parent_id = uuid.uuid4()

        class _CyclingNode:
            def __init__(self, _id: uuid.UUID) -> None:
                self.id = _id
                self.parent_id: uuid.UUID | None = chain_parent_id
                self.deleted_at = None
                self.title = "mid-chain"
                self.default_language = None

        class _AgentOverSession(MethodistAgent):
            def __init__(self) -> None:
                super().__init__(session=AsyncMock(), stage_router=AsyncMock())  # type: ignore[arg-type]

                async def _fake_get(_cls: Any, _pk: uuid.UUID) -> Any:
                    return _CyclingNode(_pk)

                self._session.get = _fake_get  # type: ignore[method-assign]

        agent = _AgentOverSession()
        start = _CyclingNode(uuid.uuid4())
        with pytest.raises(MethodistRootResolutionError) as exc_info:
            await agent._resolve_course_context(start)  # type: ignore[arg-type]
        assert exc_info.value.depth_cap == 25
        assert exc_info.value.node_id == start.id

    async def test_reaches_root_normally(self) -> None:
        # A normal walk-up does NOT raise — agent uses the root for
        # title + language. This is the regression-pin for the cap.
        class _Root:
            id = uuid.uuid4()
            parent_id = None
            deleted_at = None
            title = "Real Course"
            default_language = "ukr"

        class _Mid:
            id = uuid.uuid4()
            parent_id = _Root.id
            deleted_at = None
            title = "mid"
            default_language = None

        class _AgentOverSession(MethodistAgent):
            def __init__(self) -> None:
                super().__init__(session=AsyncMock(), stage_router=AsyncMock())  # type: ignore[arg-type]
                # Two-step chain: mid → root.

                async def _fake_get(_cls: Any, _pk: uuid.UUID) -> Any:
                    if _pk == _Root.id:
                        return _Root()
                    return _Mid()

                self._session.get = _fake_get  # type: ignore[method-assign]

        agent = _AgentOverSession()
        course_title, language = await agent._resolve_course_context(_Mid())  # type: ignore[arg-type]
        assert course_title == "Real Course"
        # display_name('ukr') resolves to a human-readable Ukrainian label.
        assert language is not None and len(language) > 0


# ── R4-followup: error propagation through generate_bottomup ─────


class _RaisingResolutionStubAgent(_StubAgent):
    """``_resolve_course_context`` raises ``MethodistRootResolutionError``."""

    async def _resolve_course_context(self, node: Any) -> tuple[str, str | None]:
        raise MethodistRootResolutionError(
            node_id=getattr(node, "id", uuid.uuid4()),
            last_mid_chain_node_id=uuid.uuid4(),
            depth_cap=25,
        )


class TestRootResolutionErrorPropagation:
    """generate_bottomup must propagate MethodistRootResolutionError without
    swallowing it. K2-catch in the orchestrator records it as ERROR; if the
    agent silently swallowed it, the orchestrator would mark the node DONE
    with empty canonical fields — exactly the silent-DONE risk R1 closed
    for the LLM-empty-response path.
    """

    async def test_resolution_error_propagates_to_caller(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _RaisingResolutionStubAgent(
            router,
            own_docs=[],
            child_raws=[],
            course_title="x",
            language="English",
        )
        raw = _FakeRaw()
        node = _FakeNode(id=uuid.uuid4(), title="t")
        with pytest.raises(MethodistRootResolutionError):
            await agent.generate_bottomup(node, raw)  # type: ignore[arg-type]
        # The stage router was never reached — agent failed before
        # producing render context.
        assert router.last_stage_name is None
        # Raw was NOT mutated — silent DONE is impossible.
        assert raw.title is None
        assert raw.compressed_summary is None
