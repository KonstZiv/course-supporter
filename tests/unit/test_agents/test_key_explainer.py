"""The key-explanation agent's compute path (mentor-rebuild tasks 06 and 07).

A :class:`_FakeStageRouter` invokes the agent's ``response_validator`` with
canned content — no real model, no database, no cost. The double is the one
from the criteria-decomposer tests, kept the same on purpose: two agents with
the same shape should be testable the same way.

What is worth testing here is not the happy path but the four ways a model can
answer plausibly and wrongly: skip a question, invent one, leave one blank,
answer in prose. Each of them would reach a student as a missing or empty
explanation, and each is refused in code rather than asked against in the
prompt alone. Task 07 adds a second field with the same rule: a doubt flag for
exactly the key's questions, or the response is sent back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from course_supporter.agents.key_explainer import (
    KEY_EXPLANATION_CEILING_USD,
    STAGE_NAME,
    KeyExplainerAgent,
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_TEXT = "1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так\n\n3. Третє?\nа) так"


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


_EXPLANATIONS = {
    "1": "У питанні йдеться про протилежне, і саме це описує цей варіант.",
    "2": "Лише цей варіант описує дію, про яку питають.",
    "3": "Решта варіантів стосуються іншого, а цей — саме того, про що питання.",
}
_NO_DOUBT = {"1": False, "2": False, "3": False}


def _response(
    explanations: dict[str, str] | None = None,
    doubts: dict[str, object] | None = None,
) -> str:
    """A prompt-v2 response: explanations and doubt flags."""
    return json.dumps(
        {
            "explanations": _EXPLANATIONS if explanations is None else explanations,
            "doubts": _NO_DOUBT if doubts is None else doubts,
        },
        ensure_ascii=False,
    )


_VALID = _response()


def _agent(canned: str, *, exc: Exception | None = None) -> KeyExplainerAgent:
    router = _FakeStageRouter(canned_response=canned, exception_on_call=exc)
    agent = KeyExplainerAgent(router)  # type: ignore[arg-type]
    agent._fake = router  # type: ignore[attr-defined]  # test handle
    return agent


class TestTheHappyPath:
    async def test_it_returns_one_explanation_per_question(self) -> None:
        agent = _agent(_VALID)

        written = await agent.explain(
            task_text=_TEXT, answers=_KEY, language="Ukrainian"
        )

        assert set(written.explanations) == {"1", "2", "3"}
        assert all(text.strip() for text in written.explanations.values())

    async def test_it_runs_the_stage_the_ladder_declares(self) -> None:
        agent = _agent(_VALID)

        await agent.explain(task_text=_TEXT, answers=_KEY, language="Ukrainian")

        assert agent._fake.last_stage_name == STAGE_NAME  # type: ignore[attr-defined]
        assert STAGE_NAME == "key_explanation"

    async def test_the_course_language_reaches_the_prompt(self) -> None:
        """The explanations are read by students, so the language is not optional."""
        agent = _agent(_VALID)

        await agent.explain(task_text=_TEXT, answers=_KEY, language="Ukrainian")

        context = agent._fake.last_kwargs  # type: ignore[attr-defined]
        assert context["language"] == "Ukrainian"
        assert context["task_text"] == _TEXT

    async def test_the_answers_reach_the_prompt_in_question_order(self) -> None:
        """Sorted by number, so 10 follows 9 rather than 1."""
        agent = _agent(
            _response(
                {str(n): "бо так" for n in (1, 2, 10)},
                {str(n): False for n in (1, 2, 10)},
            )
        )

        await agent.explain(
            task_text=_TEXT,
            answers={"10": ["а"], "1": ["б"], "2": ["в"]},
            language=None,
        )

        rendered = agent._fake.last_kwargs["answers"]  # type: ignore[attr-defined]
        assert json.loads(rendered) == {"1": ["б"], "2": ["в"], "10": ["а"]}
        assert list(json.loads(rendered)) == ["1", "2", "10"]


class TestAPlausibleButWrongAnswerIsRefused:
    async def test_a_missing_question_is_refused(self) -> None:
        """A skipped question would reach the student as no explanation at all."""
        agent = _agent(json.dumps({"explanations": {"1": "бо так", "2": "бо ні"}}))

        with pytest.raises(StructuralRetryError) as exc:
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert "missing ['3']" in str(exc.value)

    async def test_an_extra_question_is_refused(self) -> None:
        """An invented question explains something the test never asked."""
        agent = _agent(
            json.dumps(
                {"explanations": {"1": "a", "2": "b", "3": "c", "4": "invented"}}
            )
        )

        with pytest.raises(StructuralRetryError) as exc:
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert "unexpected ['4']" in str(exc.value)

    async def test_an_empty_explanation_is_refused(self) -> None:
        """Whitespace passes JSON validation and says nothing to a student."""
        agent = _agent(json.dumps({"explanations": {"1": "a", "2": "   ", "3": "c"}}))

        with pytest.raises(StructuralRetryError) as exc:
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert "question 2" in str(exc.value)

    async def test_prose_instead_of_json_is_refused(self) -> None:
        agent = _agent("Ось пояснення: перше питання — відповідь б, бо...")

        with pytest.raises(StructuralRetryError):
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

    async def test_the_right_shape_with_the_wrong_field_is_refused(self) -> None:
        """``extra='forbid'``: a model that renames the field has not answered."""
        agent = _agent(json.dumps({"explanation": {"1": "a", "2": "b", "3": "c"}}))

        with pytest.raises(StructuralRetryError):
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)


class TestTheDoubts:
    """Task 07: a doubt flag per question, checked in code like the explanations."""

    async def test_only_the_doubted_questions_come_back_each_as_true(self) -> None:
        """The doubted question keeps its explanation; the flag travels apart."""
        agent = _agent(_response(doubts={"1": False, "2": True, "3": False}))

        written = await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert written.doubts == {"2": True}
        assert set(written.explanations) == {"1", "2", "3"}

    async def test_no_doubt_at_all_is_an_empty_map(self) -> None:
        agent = _agent(_VALID)

        written = await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert written.doubts == {}

    @pytest.mark.parametrize(
        ("doubts", "named"),
        [
            (None, "missing ['1', '2', '3']"),
            ({"1": False, "2": True}, "missing ['3']"),
            ({"1": False, "2": False, "3": False, "4": True}, "unexpected ['4']"),
        ],
        ids=["no-doubts-at-all", "a-question-without-a-flag", "an-invented-question"],
    )
    async def test_a_response_without_doubts_or_with_some_missing_is_retried(
        self, doubts: dict[str, bool] | None, named: str
    ) -> None:
        """Silence is not "no doubt": an unasked question is refused, not read."""
        payload: dict[str, object] = {"explanations": _EXPLANATIONS}
        if doubts is not None:
            payload["doubts"] = doubts
        agent = _agent(json.dumps(payload, ensure_ascii=False))

        with pytest.raises(StructuralRetryError) as exc:
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

        assert "doubts must cover exactly the questions" in str(exc.value)
        assert named in str(exc.value)

    async def test_a_doubt_that_is_not_a_boolean_is_refused(self) -> None:
        agent = _agent(_response(doubts={"1": "можливо", "2": False, "3": False}))

        with pytest.raises(StructuralRetryError):
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)


class TestWhatTheCallerSees:
    async def test_ladder_exhaustion_propagates(self) -> None:
        """The caller marks the version failed; the agent does not swallow it."""
        agent = _agent(
            _VALID, exc=LadderExhaustedError(stage_name=STAGE_NAME, attempts=[])
        )

        with pytest.raises(LadderExhaustedError):
            await agent.explain(task_text=_TEXT, answers=_KEY, language=None)

    def test_the_ceiling_is_a_number_the_funds_port_can_use(self) -> None:
        """One attempt's worst case, from the registry — not the ladder's budget."""
        assert pytest.approx(0.08) == KEY_EXPLANATION_CEILING_USD
        assert 0.0757 < KEY_EXPLANATION_CEILING_USD < 0.5, (
            "above the dearest rung's single attempt, far below the live-run cap"
        )
