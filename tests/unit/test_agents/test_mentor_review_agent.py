"""Unit tests for MentorReviewAgent (sprint-mentor T6).

Pure compute-path tests: a :class:`_FakeStageRouter` invokes the agent's
``response_validator`` with canned content and returns a content-bearing
result (no real LLM, no DB), mirroring the criteria_decomposer test doubles.
Covers each phase's happy path, the stage names + render context, and the
semantic-minimum guards.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from course_supporter.agents.mentor_review import (
    STAGE_DENOISING,
    STAGE_INDUSTRY,
    STAGE_NODE_COURSE,
    STAGE_SYNTHESIS,
    MentorReviewAgent,
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)


class _FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStageRouter:
    """Captures render_context, runs the validator, returns canned content."""

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
        return _FakeResult(self.canned_response)


def _agent(canned: str, *, exc: Exception | None = None) -> MentorReviewAgent:
    router = _FakeStageRouter(canned_response=canned, exception_on_call=exc)
    agent = MentorReviewAgent(router)  # type: ignore[arg-type]
    agent._fake = router  # type: ignore[attr-defined]  # test handle
    return agent


_LAYER = {"score": 80, "rationale": "solid", "strengths": ["a"], "weaknesses": ["b"]}
_NODE_COURSE = json.dumps({"node": _LAYER, "course": {**_LAYER, "score": 70}})
_INDUSTRY = json.dumps({"industry": {**_LAYER, "score": 60}})
_DENOISE = json.dumps(
    {
        "recidivism": [
            {"signal": "off-by-one", "prior_submission_id": "s1", "note": "again"}
        ],
        "corrections": [],
        "delta": -3,
        "summary": "Repeated one earlier mistake.",
    }
)

_NODE_SUMMARY: dict[str, Any] = {
    "title": "Recursion",
    "description": "...",
    "learning_objectives": ["o1"],
    "success_criteria": ["s1"],
    "common_mistakes": ["m1"],
}
_COURSE_SUMMARY: dict[str, Any] = {
    "title": "Algorithms",
    "description": "...",
    "enclosing_context": "ctx",
    "success_criteria": ["cs1"],
}


class TestEvaluateNodeCourse:
    async def test_happy_path_returns_two_layers(self) -> None:
        agent = _agent(_NODE_COURSE)
        result = await agent.evaluate_node_course(
            task_title="T",
            task_description="D",
            task_text="X",
            criteria=[{"statement": "s", "evidence": "e"}],
            node_summary=_NODE_SUMMARY,
            course_summary=_COURSE_SUMMARY,
            author_mentor_notes="focus on edge cases",
            submission_text="def f(): ...",
            language="en",
        )
        assert result.node.score == 80
        assert result.course.score == 70
        fake: _FakeStageRouter = agent._fake  # type: ignore[attr-defined]
        assert fake.last_stage_name == STAGE_NODE_COURSE
        assert fake.last_kwargs is not None
        assert fake.last_kwargs["author_mentor_notes"] == "focus on edge cases"
        assert fake.last_kwargs["submission_text"] == "def f(): ..."

    async def test_invalid_json_raises_structural_retry(self) -> None:
        agent = _agent("not json")
        with pytest.raises(StructuralRetryError):
            await _call_node_course(agent)

    async def test_blank_rationale_raises(self) -> None:
        bad = json.dumps({"node": {**_LAYER, "rationale": "  "}, "course": _LAYER})
        agent = _agent(bad)
        with pytest.raises(StructuralRetryError, match="rationale"):
            await _call_node_course(agent)

    async def test_extra_field_rejected(self) -> None:
        bad = json.dumps({"node": {**_LAYER, "weight": 0.5}, "course": _LAYER})
        agent = _agent(bad)
        with pytest.raises(StructuralRetryError):
            await _call_node_course(agent)

    async def test_score_out_of_range_rejected(self) -> None:
        bad = json.dumps({"node": {**_LAYER, "score": 150}, "course": _LAYER})
        agent = _agent(bad)
        with pytest.raises(StructuralRetryError):
            await _call_node_course(agent)


class TestEvaluateIndustry:
    async def test_happy_path_returns_layer(self) -> None:
        agent = _agent(_INDUSTRY)
        judgment = await agent.evaluate_industry(
            task_title="T",
            task_description="D",
            task_text="X",
            submission_text="...",
            language="en",
        )
        assert judgment.score == 60
        fake: _FakeStageRouter = agent._fake  # type: ignore[attr-defined]
        assert fake.last_stage_name == STAGE_INDUSTRY
        # Course-free: no criteria / summaries / author notes leak into 1B.
        assert fake.last_kwargs is not None
        assert "criteria" not in fake.last_kwargs
        assert "node_summary" not in fake.last_kwargs
        assert "author_mentor_notes" not in fake.last_kwargs


class TestReconcile:
    async def test_happy_path(self) -> None:
        agent = _agent(_DENOISE)
        outcome = await agent.reconcile(
            aggregate_score=75,
            current_layers=[{"layer": "node", "score": 80, "weaknesses": ["b"]}],
            history=[
                {"submission_id": "s1", "score": 70, "weaknesses": ["off-by-one"]}
            ],
            denoise_cap=10,
            language="en",
        )
        assert outcome.delta == -3
        assert outcome.recidivism[0].prior_submission_id == "s1"
        fake: _FakeStageRouter = agent._fake  # type: ignore[attr-defined]
        assert fake.last_stage_name == STAGE_DENOISING
        assert fake.last_kwargs is not None
        assert fake.last_kwargs["denoise_cap"] == 10

    async def test_empty_history_zero_delta(self) -> None:
        canned = json.dumps(
            {
                "recidivism": [],
                "corrections": [],
                "delta": 0,
                "summary": "No prior attempts.",
            }
        )
        agent = _agent(canned)
        outcome = await agent.reconcile(
            aggregate_score=75,
            current_layers=[],
            history=[],
            denoise_cap=10,
            language=None,
        )
        assert outcome.delta == 0
        assert outcome.recidivism == []

    async def test_blank_summary_raises(self) -> None:
        canned = json.dumps(
            {"recidivism": [], "corrections": [], "delta": 0, "summary": "  "}
        )
        agent = _agent(canned)
        with pytest.raises(StructuralRetryError, match="summary"):
            await agent.reconcile(
                aggregate_score=75,
                current_layers=[],
                history=[],
                denoise_cap=10,
                language=None,
            )

    async def test_blank_match_field_raises(self) -> None:
        canned = json.dumps(
            {
                "recidivism": [
                    {"signal": "  ", "prior_submission_id": "s1", "note": "n"}
                ],
                "corrections": [],
                "delta": -2,
                "summary": "x",
            }
        )
        agent = _agent(canned)
        with pytest.raises(StructuralRetryError, match="empty signal or note"):
            await agent.reconcile(
                aggregate_score=75,
                current_layers=[],
                history=[],
                denoise_cap=10,
                language=None,
            )


class TestSynthesize:
    async def test_returns_stripped_markdown(self) -> None:
        agent = _agent("  # Review\n\nGood work.  ")
        text = await agent.synthesize(
            layers=[{"layer": "node", "score": 80, "strengths": [], "weaknesses": []}],
            aggregate_score=75,
            denoised_score=72,
            history_reconciliation={"recidivism": [], "corrections": []},
            score_signals=[
                {"layer": "node", "direction": "up", "magnitude": 4, "reason": "r"}
            ],
            student_note="please be kind",
            language="en",
        )
        assert text == "# Review\n\nGood work."
        fake: _FakeStageRouter = agent._fake  # type: ignore[attr-defined]
        assert fake.last_stage_name == STAGE_SYNTHESIS

    async def test_empty_review_raises(self) -> None:
        agent = _agent("   ")
        with pytest.raises(StructuralRetryError, match="non-empty"):
            await agent.synthesize(
                layers=[],
                aggregate_score=50,
                denoised_score=50,
                history_reconciliation={"recidivism": [], "corrections": []},
                score_signals=[],
                student_note=None,
                language=None,
            )


class TestLadderExhaustion:
    async def test_propagates(self) -> None:
        exc = LadderExhaustedError(stage_name=STAGE_NODE_COURSE, attempts=[])
        agent = _agent(_NODE_COURSE, exc=exc)
        with pytest.raises(LadderExhaustedError):
            await _call_node_course(agent)


async def _call_node_course(agent: MentorReviewAgent) -> Any:
    return await agent.evaluate_node_course(
        task_title="T",
        task_description="D",
        task_text="X",
        criteria=[],
        node_summary=_NODE_SUMMARY,
        course_summary=_COURSE_SUMMARY,
        author_mentor_notes=None,
        submission_text="...",
        language=None,
    )
