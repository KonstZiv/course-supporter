"""The criteria evaluation agent (mentor-rebuild task 09b, K3).

What the agent owns and nothing else: the request it renders, the schema it
hands the router, and the form it holds an answer to. The locks:

* the schema on every request is the strict schema of ``EvaluationAnswer``
  with no ``description`` at any depth — the prompt is the one source of
  instructions for the model (vision-side, 2026-10-02) — and every request
  expects JSON;
* the first request names every item of the list; the repeat names its own
  items alone, each with what was wrong and what the model said;
* an answer with an item missing, unknown or repeated is a structural refusal
  — through the real router, a retry on the same rung (``TASK.md`` lock 4's
  neighbour: a foreign identifier never becomes a verdict);
* an answer cut off at the output ceiling stops the stage after one paid
  call: no structural retry, no descent (task 09a, applied to this stage);
* the prompt carries the word "json" in every form it is rendered in, which
  JSON mode of an OpenAI-compatible provider (the DeepSeek rung) demands;
* the prompt tells the model that an item needing a file that was not read is
  "not met" with a sentence naming the file as not read, never one saying the
  student did not do the work (vision-side, 2026-10-02).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.agents.criteria_evaluator import (
    RESPONSE_SCHEMA,
    CriteriaEvaluatorAgent,
    EvaluationInput,
    render_context,
)
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import (
    EvaluationAnswer,
    RepeatItem,
    RepeatReason,
)
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    LadderStop,
    StructuralRetryError,
)
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.response_schema import mentions_json, strict_json_schema
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode
from course_supporter.llm.stage_router import StageExecution, StageResult, StageRouter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPT_REF = "prompts/criteria_evaluation/v1.md"


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


_SHOWN = EvaluationInput(
    task_title="Fibonacci",
    task_description="Recursion.",
    task_text="Write a recursive fibonacci.",
    criteria=(_criterion("c1", "must"), _criterion("c2", "should", points=2)),
    submission_text="def fibonacci(n):\n    return n\n",
    language="Ukrainian",
)
_ITEMS = ["c1", "c2.p1", "c2.p2"]


def _verdict(item: str, verdict: str = "met") -> dict[str, Any]:
    met = verdict == "met"
    return {
        "id": item,
        "verdict": verdict,
        "quote": "def fibonacci(n):" if met else None,
        "missing": None if met else "Немає базового випадку.",
    }


def _answer(*items: str) -> str:
    return json.dumps({"verdicts": [_verdict(i) for i in items]}, ensure_ascii=False)


def _execution() -> StageExecution:
    return StageExecution(
        stage=StageConfig(
            prompt_ref=_PROMPT_REF,
            requires=["json_mode"],
            ladder=[
                LadderEntry(
                    provider="dashscope", model="qwen3.7-max", max_output_tokens=16384
                ),
                LadderEntry(
                    provider="deepseek_thinking",
                    model="deepseek-v4-pro",
                    max_output_tokens=32768,
                ),
            ],
        ),
        stage_name="criteria_evaluation",
        stop_on_output_ceiling=True,
        money_ceiling_usd=0.55,
    )


class _Router:
    """Router double: records each request, runs the validator, answers."""

    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.requests: list[dict[str, Any]] = []

    async def execute_stage(
        self,
        stage: StageConfig,
        stage_name: str,
        /,
        *,
        response_validator: Callable[[str], None] | None = None,
        contents: Any = None,
        expects_json: bool = False,
        response_schema: dict[str, Any] | None = None,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render: Any,
    ) -> StageResult:
        self.requests.append(
            {
                "stage": stage,
                "expects_json": expects_json,
                "response_schema": response_schema,
                "render": render,
            }
        )
        content = self._contents.pop(0)
        if response_validator is not None:
            response_validator(content)
        rung = stage.ladder[0]
        return StageResult(
            content=content,
            provider_used=rung.provider,
            model_used=rung.model,
            attempt_count=1,
        )


def _keywords(node: Any, found: set[str] | None = None) -> set[str]:
    """Every keyword a schema uses at any depth; the names of fields are not."""
    found = set() if found is None else found
    if isinstance(node, list):
        for item in node:
            _keywords(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            for sub in value.values() if key == "properties" else [value]:
                _keywords(sub, found)
    return found


class TestTheSchemaOnTheWire:
    """Vision-side, 2026-10-02: no developer's docstring reaches the model."""

    def test_no_description_is_left_at_any_depth(self) -> None:
        strict = strict_json_schema(EvaluationAnswer)
        # The premise: pydantic does put the docstrings there, at three depths.
        assert "description" in strict
        assert "description" in strict["properties"]["verdicts"]["items"]
        assert "description" in _keywords(strict)

        assert "description" not in _keywords(RESPONSE_SCHEMA)

    def test_the_form_itself_is_kept(self) -> None:
        item = RESPONSE_SCHEMA["properties"]["verdicts"]["items"]
        assert RESPONSE_SCHEMA["required"] == ["verdicts"]
        assert RESPONSE_SCHEMA["additionalProperties"] is False
        assert item["required"] == ["id", "verdict", "quote", "missing"]
        assert item["additionalProperties"] is False
        assert item["properties"]["verdict"]["enum"] == ["met", "not_met"]
        assert item["properties"]["quote"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]


class TestTheRequest:
    async def test_every_request_carries_the_strict_schema_of_the_answer(
        self,
    ) -> None:
        router = _Router(_answer(*_ITEMS), _answer("c1"))
        agent = CriteriaEvaluatorAgent(router)  # type: ignore[arg-type]

        first = await agent.evaluate(_SHOWN, execution=_execution())
        await agent.ask_again(
            _SHOWN,
            [RepeatItem("c1", RepeatReason.QUOTE_NOT_FOUND)],
            first.answer,
            execution=_execution(),
        )

        assert _keywords(RESPONSE_SCHEMA) <= _keywords(
            strict_json_schema(EvaluationAnswer)
        )
        assert len(router.requests) == 2
        for request in router.requests:
            assert request["response_schema"] == RESPONSE_SCHEMA
            assert request["expects_json"] is True

    async def test_the_first_request_names_every_item_of_the_list(self) -> None:
        router = _Router(_answer(*_ITEMS))

        evaluated = await CriteriaEvaluatorAgent(router).evaluate(  # type: ignore[arg-type]
            _SHOWN, execution=_execution()
        )

        (request,) = router.requests
        assert request["render"]["items"] == _ITEMS
        assert request["render"]["repeat"] == []
        assert [v.id for v in evaluated.answer.verdicts] == _ITEMS
        assert (evaluated.provider, evaluated.model) == ("dashscope", "qwen3.7-max")

    async def test_the_repeat_names_its_items_alone_each_with_its_reason(
        self,
    ) -> None:
        first = EvaluationAnswer.model_validate_json(
            json.dumps(
                {
                    "verdicts": [
                        _verdict("c1"),
                        {**_verdict("c2.p1"), "quote": "return  n * 3"},
                        _verdict("c2.p2", "not_met") | {"missing": None},
                    ]
                }
            )
        )
        repeat = [
            RepeatItem("c2.p1", RepeatReason.QUOTE_NOT_FOUND),
            RepeatItem("c2.p2", RepeatReason.NO_SENTENCE),
        ]
        router = _Router(_answer("c2.p1", "c2.p2"))

        await CriteriaEvaluatorAgent(router).ask_again(  # type: ignore[arg-type]
            _SHOWN, repeat, first, execution=_execution()
        )

        (request,) = router.requests
        assert request["render"]["items"] == ["c2.p1", "c2.p2"]
        assert request["render"]["repeat"] == [
            {
                "id": "c2.p1",
                "problem": RepeatItem("c2.p1", RepeatReason.QUOTE_NOT_FOUND).feedback,
                "your_answer": {
                    "verdict": "met",
                    "quote": "return  n * 3",
                    "missing": None,
                },
            },
            {
                "id": "c2.p2",
                "problem": RepeatItem("c2.p2", RepeatReason.NO_SENTENCE).feedback,
                "your_answer": {"verdict": "not_met", "quote": None, "missing": None},
            },
        ]

    def test_points_are_shown_only_on_a_criterion_checked_by_them(self) -> None:
        context = render_context(_SHOWN, expected=_ITEMS, repeat=[])

        c1, c2 = context["criteria"]
        assert "mandatory_points" not in c1
        assert c2["mandatory_points"] == [
            {"id": "c2.p1", "text": "Point 1 of c2"},
            {"id": "c2.p2", "text": "Point 2 of c2"},
        ]
        assert c1["weight"] == "must"


class TestTheFormOfTheAnswerIsHeld:
    @pytest.mark.parametrize(
        ("items", "fault"),
        [
            pytest.param(
                [*_ITEMS, "c9"], "unexpected ['c9']", id="a-foreign-identifier"
            ),
            pytest.param(["c1", "c2.p1"], "missing ['c2.p2']", id="a-missing-item"),
            pytest.param([*_ITEMS, "c1"], "repeated ['c1']", id="a-repeated-item"),
        ],
    )
    async def test_the_first_answer_is_refused_whole(
        self, items: list[str], fault: str
    ) -> None:
        router = _Router(_answer(*items))

        with pytest.raises(StructuralRetryError, match=fault.replace("[", r"\[")):
            await CriteriaEvaluatorAgent(router).evaluate(  # type: ignore[arg-type]
                _SHOWN, execution=_execution()
            )

    @pytest.mark.parametrize(
        ("items", "fault"),
        [
            pytest.param(
                ["c2.p1", "c1"], "unexpected ['c1']", id="an-item-not-asked-again"
            ),
            pytest.param([], "missing ['c2.p1']", id="the-item-asked-again-left-out"),
        ],
    )
    async def test_the_repeat_is_held_to_its_own_items(
        self, items: list[str], fault: str
    ) -> None:
        router = _Router(_answer(*items))
        first = EvaluationAnswer.model_validate_json(_answer(*_ITEMS))

        with pytest.raises(StructuralRetryError, match=fault.replace("[", r"\[")):
            await CriteriaEvaluatorAgent(router).ask_again(  # type: ignore[arg-type]
                _SHOWN,
                [RepeatItem("c2.p1", RepeatReason.QUOTE_NOT_FOUND)],
                first,
                execution=_execution(),
            )


def _provider(*responses: LLMResponse) -> Any:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(responses))
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


def _response(content: str, finish: FinishReason = FinishReason.STOP) -> LLMResponse:
    return LLMResponse(
        content=content, provider="p", model_id="m", finish_reason=finish
    )


def _real_router(qwen: Any, deepseek: Any) -> StageRouter:
    """The real router over the real registry; only the two providers are fakes."""
    return StageRouter(
        LadderConfig(),
        {"dashscope": qwen, "deepseek_thinking": deepseek},
        registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
        prompt_base_path=_REPO_ROOT,
    )


class TestThroughTheRealRouter:
    async def test_a_foreign_identifier_is_retried_on_the_same_rung(self) -> None:
        qwen = _provider(_response(_answer(*_ITEMS, "c9")), _response(_answer(*_ITEMS)))
        deepseek = _provider()

        evaluated = await CriteriaEvaluatorAgent(_real_router(qwen, deepseek)).evaluate(
            _SHOWN, execution=_execution()
        )

        assert qwen.complete.await_count == 2
        deepseek.complete.assert_not_awaited()
        assert [v.id for v in evaluated.answer.verdicts] == _ITEMS

    async def test_the_schema_reaches_each_rung_in_its_own_form(self) -> None:
        """qwen3.7-max holds a strict schema; DeepSeek gets JSON mode (09a)."""
        qwen = _provider(_response(_answer("c1")), _response(_answer("c1")))
        deepseek = _provider(_response(_answer(*_ITEMS)))

        await CriteriaEvaluatorAgent(_real_router(qwen, deepseek)).evaluate(
            _SHOWN, execution=_execution()
        )

        sent_to_qwen: LLMRequest = qwen.complete.await_args_list[0].args[0]
        sent_to_deepseek: LLMRequest = deepseek.complete.await_args.args[0]
        assert sent_to_qwen.schema_mode is SchemaMode.STRICT
        assert sent_to_qwen.response_schema == RESPONSE_SCHEMA
        assert sent_to_qwen.response_schema is not None
        assert "description" not in _keywords(sent_to_qwen.response_schema)
        assert sent_to_deepseek.schema_mode is SchemaMode.JSON
        assert mentions_json(sent_to_deepseek)

    async def test_an_answer_cut_at_the_output_ceiling_stops_after_one_call(
        self,
    ) -> None:
        cut = _answer(*_ITEMS)[:40]
        qwen = _provider(_response(cut, FinishReason.OUTPUT_CEILING))
        deepseek = _provider()

        with pytest.raises(LadderExhaustedError) as caught:
            await CriteriaEvaluatorAgent(_real_router(qwen, deepseek)).evaluate(
                _SHOWN, execution=_execution()
            )

        assert caught.value.stop is LadderStop.OUTPUT_CEILING
        assert qwen.complete.await_count == 1
        deepseek.complete.assert_not_awaited()


class TestThePromptSaysJson:
    """JSON mode on DeepSeek refuses a request whose messages lack the word."""

    @pytest.mark.parametrize("language", ["Ukrainian", None])
    @pytest.mark.parametrize(
        "repeat",
        [
            pytest.param([], id="first"),
            pytest.param(
                [{"id": "c1", "problem": "p", "your_answer": {}}], id="repeat"
            ),
        ],
    )
    def test_every_rendering_mentions_json(
        self, language: str | None, repeat: list[dict[str, Any]]
    ) -> None:
        shown = replace(_SHOWN, language=language)
        rendered = load_prompt(_PROMPT_REF, base_path=_REPO_ROOT).render(
            **render_context(shown, expected=_ITEMS, repeat=repeat)
        )
        request = LLMRequest(
            prompt=rendered.user or "",
            system_prompt=rendered.system,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA,
            schema_mode=SchemaMode.JSON,
        )

        assert mentions_json(request)


class TestThePromptOnUnreadFiles:
    """Vision-side, 2026-10-02: not read is not the same as not done."""

    _RULE = (
        "An item that needs a file that was not read is `not_met`, and its "
        "`missing` sentence names that file as not read — it never says the "
        "student did not do the work."
    )

    @pytest.mark.parametrize("language", ["Ukrainian", None])
    @pytest.mark.parametrize(
        "repeat",
        [
            pytest.param([], id="first"),
            pytest.param(
                [{"id": "c1", "problem": "p", "your_answer": {}}], id="repeat"
            ),
        ],
    )
    def test_every_rendering_carries_the_rule(
        self, language: str | None, repeat: list[dict[str, Any]]
    ) -> None:
        rendered = load_prompt(_PROMPT_REF, base_path=_REPO_ROOT).render(
            **render_context(
                replace(_SHOWN, language=language), expected=_ITEMS, repeat=repeat
            )
        )

        assert self._RULE in " ".join((rendered.system or "").split())
