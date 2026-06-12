"""Unit tests for :class:`MethodistAgent.generate_topdown` (Phase 3.2.3b).

Mirror :mod:`test_methodist_bottomup` test doubles — :class:`_FakeStageRouter`
captures the render-context and feeds canned content through the
response_validator; the :class:`_StubAgent` overrides
``_resolve_course_context`` so no real DB read fires. Tests pin:

* S3 input contract — render-context contains node + 6-field parent
  subset + ``parent_enclosing_context`` ONLY; NO ``parent.compressed_summary``,
  NO siblings.
* nullable parent enclosing context — children-of-root case renders ``None``.
* semantic minimum — empty ``enclosing_context`` → ``StructuralRetryError``.
* observations merge — Pass 1 list is **extended**, not replaced (Q1 ratify).
* canonical fields untouched — Pass 2 mutates only ``enclosing_context``
  and ``methodist_observations`` (R3 invariant).
* input-budget exhaustion translated to
  :class:`MethodistInputBudgetExhaustedError` with ``pass_label="Pass 2"``.
* mixed-cause exhaustion re-raises the original :class:`LadderExhaustedError`.
* invalid JSON → ``StructuralRetryError`` (validator schema check).
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
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)

# ── Fake DB rows ─────────────────────────────────────────────────


@dataclass
class _FakeRaw:
    """Mutable target for in-place Pass 2 writes.

    Mirrors :class:`NodeSummaryRaw` shape sufficient for Pass 2: the
    canonical fields that ``generate_topdown`` READS from the node's own
    ``raw`` and from ``parent_raw`` plus the two fields it WRITES
    (``enclosing_context`` + ``methodist_observations``).
    """

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
    main_concepts: list[str] = field(default_factory=list)
    secondary_concepts: list[str] = field(default_factory=list)
    own_documents_count: int = 0
    own_chars_count: int = 0
    cumulative_documents_count: int = 0
    cumulative_chars_count: int = 0
    methodist_observations: list[str] = field(default_factory=list)
    enclosing_context: str | None = None


@dataclass
class _FakeNode:
    id: uuid.UUID
    title: str
    parent_id: uuid.UUID | None = None


# ── FakeStageRouter ──────────────────────────────────────────────


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


# ── Stub the agent ───────────────────────────────────────────────


class _StubAgent(MethodistAgent):
    """Override ``_resolve_course_context`` so no real DB walk fires."""

    def __init__(
        self,
        stage_router: _FakeStageRouter,
        *,
        course_title: str,
        language: str | None,
    ) -> None:
        super().__init__(session=AsyncMock(), stage_router=stage_router)  # type: ignore[arg-type]
        self._stub_course_title = course_title
        self._stub_language = language

    async def _resolve_course_context(self, node: Any) -> tuple[str, str | None]:
        del node
        return self._stub_course_title, self._stub_language


# ── Canned LLM response ──────────────────────────────────────────


_FULL_TOPDOWN_BODY = {
    "enclosing_context": (
        "Цей вузол стоїть у курсі як практичне продовження теми хеш-функцій: "
        "батьківський модуль навчає принципам незворотного перетворення, а "
        "цей підмодуль уточнює, як приходити до однакового результату на "
        "різних вхідних даних. Дітям цього вузла достатньо знати, що "
        "обчислення вже відбулось у батька; вони звертатимуться сюди як "
        "до проміжного шару, що формалізує очікування результату."
    ),
    "observations": ["перевірити співвідношення з KD2"],
}


def _llm_json(extra: dict[str, Any] | None = None) -> str:
    body = dict(_FULL_TOPDOWN_BODY)
    if extra:
        body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _make_node(title: str = "node-x") -> _FakeNode:
    return _FakeNode(id=uuid.uuid4(), title=title, parent_id=uuid.uuid4())


# ── Tests ────────────────────────────────────────────────────────


class TestS3RenderContext:
    """Render-context obeys S3: parent canonical subset + parent
    enclosing_context ONLY; never parent.compressed_summary, never siblings.
    """

    async def test_six_parent_fields_passed(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="course-X", language="Ukrainian")
        node = _make_node()
        raw = _FakeRaw(title="self", description="desc")
        parent_raw = _FakeRaw(
            title="parent-title",
            description="parent-desc",
            learning_objectives=["obj-1"],
            main_concepts=["mc-1"],
            secondary_concepts=["sc-1"],
            teaching_approach="ta-1",
            knowledge=[{"name": "k", "description": "d"}],
            skills=[{"name": "s", "description": "d"}],
            success_criteria=["fence"],
            assessment_approach="aa-1",
            key_activities=["ka-1"],
            common_mistakes=["cm-1"],
            compressed_summary="parent-compressed-PROHIBITED",
            enclosing_context="enc-ctx-parent",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        kwargs = router.last_kwargs
        assert kwargs is not None
        parent_payload = kwargs["parent"]
        assert set(parent_payload.keys()) == {
            "title",
            "description",
            "learning_objectives",
            "main_concepts",
            "secondary_concepts",
            "teaching_approach",
        }
        assert parent_payload["title"] == "parent-title"
        assert parent_payload["main_concepts"] == ["mc-1"]

    async def test_parent_compressed_summary_not_in_render_context(self) -> None:
        # Negative S3 check: every render-context value must be free of
        # the parent's compressed_summary text, regardless of which key.
        forbidden_marker = "PARENT_COMPRESSED_DO_NOT_LEAK"
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            compressed_summary=forbidden_marker,
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        kwargs = router.last_kwargs
        assert kwargs is not None
        serialised = json.dumps(kwargs, default=str)
        assert forbidden_marker not in serialised

    async def test_no_sibling_data_in_render_context(self) -> None:
        # The render-context must not contain any "siblings" / "children"
        # keys — only this node + parent subset + parent enclosing_context.
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        kwargs = router.last_kwargs
        assert kwargs is not None
        assert "siblings" not in kwargs
        assert "children" not in kwargs
        assert "children_compressed" not in kwargs
        assert "child_count" not in kwargs


class TestNullableParentEnclosingContext:
    """Children of the course root see ``parent_enclosing_context = None``."""

    async def test_passes_none_when_parent_enclosing_is_null(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="x", language="Ukrainian")
        node = _make_node()
        raw = _FakeRaw(title="self")
        # Parent IS the course root → enclosing_context=None per KD10 invariant.
        parent_raw = _FakeRaw(
            title="root",
            description="root-desc",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="ta",
            enclosing_context=None,
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        kwargs = router.last_kwargs
        assert kwargs is not None
        assert kwargs["parent_enclosing_context"] is None


class TestSemanticMinimum:
    """Empty ``enclosing_context`` must NOT pass — non-root invariant per S3."""

    async def test_empty_enclosing_context_raises_structural_retry(self) -> None:
        empty_response = json.dumps({"enclosing_context": "", "observations": []})
        router = _FakeStageRouter(canned_response=empty_response)
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(StructuralRetryError) as exc_info:
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        msg = str(exc_info.value)
        assert "enclosing_context" in msg
        assert "1000-2000" in msg or "non-empty" in msg

    async def test_whitespace_only_enclosing_context_treated_as_empty(self) -> None:
        ws_response = json.dumps(
            {"enclosing_context": "   \n\t   ", "observations": []}
        )
        router = _FakeStageRouter(canned_response=ws_response)
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(StructuralRetryError):
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]


class TestObservationsAppendSemantics:
    """Q1 ratify: Pass 2 EXTENDS the Pass 1 observations list, never replaces."""

    async def test_pass1_observations_preserved_and_pass2_appended(self) -> None:
        router = _FakeStageRouter(
            canned_response=_llm_json(
                {"observations": ["topdown-obs-1", "topdown-obs-2"]}
            )
        )
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(
            title="self",
            methodist_observations=["pass1-obs-A", "pass1-obs-B"],
        )
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        assert raw.methodist_observations == [
            "pass1-obs-A",
            "pass1-obs-B",
            "topdown-obs-1",
            "topdown-obs-2",
        ]

    async def test_empty_topdown_observations_leaves_pass1_intact(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json({"observations": []}))
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(
            title="self",
            methodist_observations=["pass1-only"],
        )
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        assert raw.methodist_observations == ["pass1-only"]


class TestCanonicalFieldsUntouched:
    """Pass 2 mutates only ``enclosing_context`` + ``methodist_observations``."""

    async def test_canonical_fields_unchanged_after_topdown(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(
            title="canonical-title",
            description="canonical-desc",
            learning_objectives=["lo-1", "lo-2"],
            knowledge=[{"name": "k1", "description": "k1-desc"}],
            skills=[{"name": "s1", "description": "s1-desc"}],
            success_criteria=["sc-1"],
            assessment_approach="aa",
            teaching_approach="ta",
            key_activities=["ka-1"],
            common_mistakes=["cm-1"],
            compressed_summary="cs",
            main_concepts=["mc"],
            secondary_concepts=["sec"],
            own_documents_count=3,
            own_chars_count=1000,
            cumulative_documents_count=5,
            cumulative_chars_count=2000,
            methodist_observations=[],
        )
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        assert raw.title == "canonical-title"
        assert raw.description == "canonical-desc"
        assert raw.learning_objectives == ["lo-1", "lo-2"]
        assert raw.knowledge == [{"name": "k1", "description": "k1-desc"}]
        assert raw.skills == [{"name": "s1", "description": "s1-desc"}]
        assert raw.success_criteria == ["sc-1"]
        assert raw.assessment_approach == "aa"
        assert raw.teaching_approach == "ta"
        assert raw.key_activities == ["ka-1"]
        assert raw.common_mistakes == ["cm-1"]
        assert raw.compressed_summary == "cs"
        assert raw.main_concepts == ["mc"]
        assert raw.secondary_concepts == ["sec"]
        assert raw.own_documents_count == 3
        assert raw.own_chars_count == 1000
        assert raw.cumulative_documents_count == 5
        assert raw.cumulative_chars_count == 2000
        # Mutated fields:
        assert raw.enclosing_context == _FULL_TOPDOWN_BODY["enclosing_context"]
        assert raw.methodist_observations == _FULL_TOPDOWN_BODY["observations"]


class TestBudgetExhaustionTranslation:
    """LadderExhaustedError → MethodistInputBudgetExhaustedError(pass_label='Pass 2')
    when every rung was skipped for input-budget reasons.
    """

    async def test_all_input_budget_skips_translates_to_domain_error(self) -> None:
        attempts = [
            ("deepseek_thinking", "deepseek-v4-pro", "input budget exceeded: ~9000"),
            ("dashscope", "qwen3.7-max", "input budget exceeded: ~9000"),
            ("deepseek", "deepseek-v4-flash", "input budget exceeded: ~9000"),
        ]
        router = _FakeStageRouter(
            canned_response="never-reached",
            exception_on_call=LadderExhaustedError(
                stage_name="methodist_topdown",
                attempts=attempts,
            ),
        )
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(MethodistInputBudgetExhaustedError) as exc_info:
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        err = exc_info.value
        assert err.pass_label == "Pass 2"
        assert err.stage_name == "methodist_topdown"
        assert err.attempts == attempts
        msg = str(err)
        assert "Pass 2" in msg
        assert "Restructure" in msg or "restructure" in msg

    async def test_mixed_cause_exhaustion_reraises_original(self) -> None:
        attempts = [
            ("deepseek_thinking", "deepseek-v4-pro", "input budget exceeded: ~9000"),
            ("dashscope", "qwen3.7-max", "provider 429 after retries"),
        ]
        original_exc = LadderExhaustedError(
            stage_name="methodist_topdown",
            attempts=attempts,
        )
        router = _FakeStageRouter(
            canned_response="never-reached",
            exception_on_call=original_exc,
        )
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(LadderExhaustedError) as exc_info:
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        # Original error surfaces — agent did NOT translate.
        assert exc_info.value is original_exc
        assert not isinstance(exc_info.value, MethodistInputBudgetExhaustedError)


class TestInvalidJSONTranslatesToStructuralRetry:
    async def test_unparseable_response_raises_structural_retry(self) -> None:
        router = _FakeStageRouter(canned_response="not-json-at-all")
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(StructuralRetryError):
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

    async def test_extra_field_rejected(self) -> None:
        # Schema is extra=forbid; surplus key triggers validation.
        extra_response = json.dumps(
            {
                "enclosing_context": _FULL_TOPDOWN_BODY["enclosing_context"],
                "observations": [],
                "title": "should-not-be-here",  # canonical field — forbidden
            }
        )
        router = _FakeStageRouter(canned_response=extra_response)
        agent = _StubAgent(router, course_title="x", language=None)
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        with pytest.raises(StructuralRetryError):
            await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]


class TestLanguageAndCourseTitleInRenderContext:
    async def test_language_and_course_title_propagate(self) -> None:
        router = _FakeStageRouter(canned_response=_llm_json())
        agent = _StubAgent(router, course_title="курс-Y", language="Ukrainian")
        node = _make_node()
        raw = _FakeRaw(title="self")
        parent_raw = _FakeRaw(
            title="p",
            description="d",
            learning_objectives=[],
            main_concepts=[],
            secondary_concepts=[],
            teaching_approach="t",
            enclosing_context="ec",
        )

        await agent.generate_topdown(node, raw, parent_raw)  # type: ignore[arg-type]

        kwargs = router.last_kwargs
        assert kwargs is not None
        assert kwargs["language"] == "Ukrainian"
        assert kwargs["course_title"] == "курс-Y"
        assert router.last_stage_name == "methodist_topdown"
