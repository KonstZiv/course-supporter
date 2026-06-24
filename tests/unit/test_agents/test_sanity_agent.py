"""Unit tests for SanityAgent (sprint-mentor T7).

Pure compute-path tests: a :class:`_FakeStageRouter` runs the agent's
``response_validator`` with canned content and returns a content-bearing result
(no real LLM, no DB), mirroring the MentorReviewAgent test doubles. Covers the
happy path (both verdicts), the stage name + render context, and the
semantic-minimum / structural guards.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from course_supporter.agents.sanity import STAGE_SANITY, SanityAgent
from course_supporter.llm.error_categories import StructuralRetryError


class _FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStageRouter:
    """Captures render_context, runs the validator, returns canned content."""

    def __init__(self, *, canned_response: str) -> None:
        self.canned_response = canned_response
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
        if response_validator is not None:
            response_validator(self.canned_response)
        return _FakeResult(self.canned_response)


def _agent(canned: str) -> tuple[SanityAgent, _FakeStageRouter]:
    router = _FakeStageRouter(canned_response=canned)
    return SanityAgent(router), router  # type: ignore[arg-type]


_CTX = {
    "task_title": "Recursion",
    "task_description": "Implement factorial recursively.",
    "task_text": "Write a recursive factorial.",
    "submission_text": "def fact(n): return 1 if n == 0 else n * fact(n - 1)",
    "language": "English",
}


class TestSanityAgentHappyPath:
    @pytest.mark.parametrize("verdict", ["match", "mismatch"])
    async def test_returns_parsed_classification(self, verdict: str) -> None:
        canned = json.dumps({"verdict": verdict, "confidence": 0.91, "reason": "clear"})
        agent, router = _agent(canned)
        result = await agent.classify(**_CTX)
        assert result.verdict == verdict
        assert result.confidence == 0.91
        assert result.reason == "clear"
        assert router.last_stage_name == STAGE_SANITY

    async def test_render_context_forwarded(self) -> None:
        canned = json.dumps({"verdict": "match", "confidence": 0.6, "reason": "ok"})
        agent, router = _agent(canned)
        await agent.classify(**_CTX)
        assert router.last_kwargs is not None
        for key, value in _CTX.items():
            assert router.last_kwargs[key] == value


class TestSanityAgentGuards:
    async def test_invalid_json_raises_structural_retry(self) -> None:
        agent, _ = _agent("not json at all")
        with pytest.raises(StructuralRetryError):
            await agent.classify(**_CTX)

    async def test_unknown_verdict_raises_structural_retry(self) -> None:
        canned = json.dumps({"verdict": "maybe", "confidence": 0.5, "reason": "x"})
        agent, _ = _agent(canned)
        with pytest.raises(StructuralRetryError):
            await agent.classify(**_CTX)

    async def test_confidence_out_of_range_raises_structural_retry(self) -> None:
        canned = json.dumps({"verdict": "match", "confidence": 1.7, "reason": "x"})
        agent, _ = _agent(canned)
        with pytest.raises(StructuralRetryError):
            await agent.classify(**_CTX)

    async def test_empty_reason_raises_structural_retry(self) -> None:
        canned = json.dumps(
            {"verdict": "mismatch", "confidence": 0.95, "reason": "   "}
        )
        agent, _ = _agent(canned)
        with pytest.raises(StructuralRetryError):
            await agent.classify(**_CTX)

    async def test_extra_field_raises_structural_retry(self) -> None:
        canned = json.dumps(
            {"verdict": "match", "confidence": 0.8, "reason": "x", "score": 50}
        )
        agent, _ = _agent(canned)
        with pytest.raises(StructuralRetryError):
            await agent.classify(**_CTX)
