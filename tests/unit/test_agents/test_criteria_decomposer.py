"""Unit tests for CriteriaDecomposerAgent (sprint-mentor T4, D11).

Pure compute-path tests: a :class:`_FakeStageRouter` invokes the agent's
``response_validator`` with canned content (no real LLM, no DB), mirroring
the methodist agent test doubles. Covers the happy path, the render
context passed to the stage, and every semantic-minimum guard.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from course_supporter.agents.criteria_decomposer import (
    STAGE_NAME,
    CriteriaDecomposerAgent,
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)


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


_VALID = json.dumps(
    {
        "criteria": [
            {"statement": "Has a base case", "evidence": "explicit if-return"},
            {"statement": "Recurses toward base", "evidence": "argument shrinks"},
        ]
    }
)


def _agent(canned: str, *, exc: Exception | None = None) -> CriteriaDecomposerAgent:
    router = _FakeStageRouter(canned_response=canned, exception_on_call=exc)
    agent = CriteriaDecomposerAgent(router)  # type: ignore[arg-type]
    agent._fake = router  # type: ignore[attr-defined]  # test handle
    return agent


class TestHappyPath:
    async def test_returns_criteria_dicts(self) -> None:
        agent = _agent(_VALID)
        criteria = await agent.decompose(
            task_title="Factorial",
            task_description="Implement factorial recursively.",
            task_text="Write a recursive factorial function...",
            task_type="task",
            language="en",
        )
        assert criteria == [
            {"statement": "Has a base case", "evidence": "explicit if-return"},
            {"statement": "Recurses toward base", "evidence": "argument shrinks"},
        ]

    async def test_passes_render_context_and_stage_name(self) -> None:
        agent = _agent(_VALID)
        await agent.decompose(
            task_title="T",
            task_description="D",
            task_text="X",
            task_type="project",
            language="uk",
        )
        fake: _FakeStageRouter = agent._fake  # type: ignore[attr-defined]
        assert fake.last_stage_name == STAGE_NAME
        assert fake.last_kwargs == {
            "task_type": "project",
            "language": "uk",
            "task_title": "T",
            "task_description": "D",
            "task_text": "X",
        }


class TestSemanticGuards:
    async def test_invalid_json_raises_structural_retry(self) -> None:
        agent = _agent("not json at all")
        with pytest.raises(StructuralRetryError):
            await agent.decompose(
                task_title="T",
                task_description="D",
                task_text="X",
                task_type="task",
                language=None,
            )

    async def test_empty_criteria_list_raises(self) -> None:
        agent = _agent(json.dumps({"criteria": []}))
        with pytest.raises(StructuralRetryError, match="criteria is empty"):
            await agent.decompose(
                task_title="T",
                task_description="D",
                task_text="X",
                task_type="task",
                language=None,
            )

    async def test_blank_field_raises(self) -> None:
        agent = _agent(json.dumps({"criteria": [{"statement": "  ", "evidence": "e"}]}))
        with pytest.raises(StructuralRetryError, match="empty statement or evidence"):
            await agent.decompose(
                task_title="T",
                task_description="D",
                task_text="X",
                task_type="task",
                language=None,
            )

    async def test_extra_field_rejected(self) -> None:
        # extra=forbid: a canonical field smuggled into the item is rejected.
        agent = _agent(
            json.dumps(
                {"criteria": [{"statement": "s", "evidence": "e", "weight": 0.5}]}
            )
        )
        with pytest.raises(StructuralRetryError):
            await agent.decompose(
                task_title="T",
                task_description="D",
                task_text="X",
                task_type="task",
                language=None,
            )


class TestLadderExhaustion:
    async def test_ladder_exhausted_propagates(self) -> None:
        exc = LadderExhaustedError(
            stage_name=STAGE_NAME,
            attempts=[],
        )
        agent = _agent(_VALID, exc=exc)
        with pytest.raises(LadderExhaustedError):
            await agent.decompose(
                task_title="T",
                task_description="D",
                task_text="X",
                task_type="task",
                language=None,
            )
