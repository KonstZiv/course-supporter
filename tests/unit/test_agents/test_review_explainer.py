"""The explanation agent and its prompt (mentor-rebuild task 09b, K5).

What the agent owns: the request it renders, the schema it hands the router,
and the check it holds every answer to. The locks:

* the schema on every request is the strict schema of ``ExplanationAnswer``
  with no ``description`` at any depth, and every request expects JSON;
* the model is shown each verdict with the fields that stand — the line and
  the place of a "met", the sentence of a "not met" — and never the quote
  that was not found;
* an explanation that disagrees with the facts is refused on every rung:
  through the real router, a retry on the same rung with the reason, then the
  next rung, then nothing (``TASK.md`` lock 7, ``PRE-FLIGHT.md`` 9.7);
* the prompt carries the word "json", which JSON mode on the DeepSeek rung
  demands, and the rules of the session that opened K5 (2026-10-02): the
  two voices, the contrasting form, the evidence the code gives, no links or
  URLs of the model's making, honesty on a quote that was not found and on a
  file that was not read, the student's language without internal terms —
  and, added before the commit, what a remark weighs, in plain words.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.agents.review_explainer import (
    RESPONSE_SCHEMA,
    ExplanationInput,
    ReviewExplainerAgent,
    render_context,
)
from course_supporter.agents.wire_schema import wire_schema
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_verdicts import QuotePlace
from course_supporter.homework.verdict_explanation import (
    CriterionFacts,
    ExplanationAnswer,
    ExplanationFacts,
    JudgedItem,
)
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.response_schema import mentions_json, strict_json_schema
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode
from course_supporter.llm.stage_router import StageExecution, StageResult, StageRouter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPT_REF = "prompts/review_explanation/v1.md"


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


# c1 "must", not met, its quote not found; c2 "should" by points, one not met;
# c3 "may", met on a document, kept by the safeguard.
_FACTS = ExplanationFacts(
    passed=False,
    score=16,
    criteria=(
        CriterionFacts(
            _criterion("c1", "must"),
            VerdictValue.NOT_MET,
            JudgedItem(id="c1", verdict=VerdictValue.NOT_MET, quote_not_found=True),
            (),
        ),
        CriterionFacts(
            _criterion("c2", "should", points=2),
            VerdictValue.NOT_MET,
            None,
            (
                JudgedItem(
                    id="c2.p1",
                    verdict=VerdictValue.MET,
                    quote="return fibonacci(n - 1)",
                    place=QuotePlace("solution.py", (4, 4)),
                ),
                JudgedItem(
                    id="c2.p2",
                    verdict=VerdictValue.NOT_MET,
                    missing="Файл tests.py не прочитано.",
                ),
            ),
        ),
        CriterionFacts(
            _criterion("c3", "may"),
            VerdictValue.MET,
            JudgedItem(
                id="c3",
                verdict=VerdictValue.MET,
                quote="Пояснення рекурсії.",
                place=QuotePlace("README.docx", None),
                kept_from_earlier=True,
            ),
            (),
        ),
    ),
)

_SHOWN = ExplanationInput(
    task_title="Fibonacci",
    task_description="Recursion.",
    task_text="Write a recursive fibonacci.",
    facts=_FACTS,
    submission_text="--- solution.py ---\ndef fibonacci(n):\n    return n\n",
    language="Ukrainian",
)


def _answer(passed: bool = False, remarks: tuple[str, ...] = ("c1", "c2")) -> str:
    return json.dumps(
        {
            "passed": passed,
            "why": "Робота ще не зарахована: бракує двох речей.",
            "remarks": [
                {
                    "id": cid,
                    "what": "Зараз у роботі цього не видно.",
                    "why": "Без цього розв'язок не перевірено.",
                    "todo": "Додайте перевірку базового випадку.",
                }
                for cid in remarks
            ],
            "mentor_voice": None,
        },
        ensure_ascii=False,
    )


def _execution() -> StageExecution:
    return StageExecution(
        stage=StageConfig(
            prompt_ref=_PROMPT_REF,
            requires=["json_mode"],
            ladder=[
                LadderEntry(
                    provider="dashscope", model="qwen3.7-max", max_output_tokens=8192
                ),
                LadderEntry(
                    provider="deepseek", model="deepseek-flash", max_output_tokens=8192
                ),
            ],
        ),
        stage_name="review_explanation",
        stop_on_output_ceiling=True,
        money_ceiling_usd=0.18,
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
    def test_it_is_the_answers_schema_without_descriptions(self) -> None:
        # The premise: pydantic does put the docstrings there.
        assert "description" in _keywords(strict_json_schema(ExplanationAnswer))

        assert wire_schema(ExplanationAnswer) == RESPONSE_SCHEMA
        assert "description" not in _keywords(RESPONSE_SCHEMA)

    def test_the_form_itself_is_kept(self) -> None:
        remark = RESPONSE_SCHEMA["properties"]["remarks"]["items"]
        assert RESPONSE_SCHEMA["required"] == [
            "passed",
            "why",
            "remarks",
            "mentor_voice",
        ]
        assert RESPONSE_SCHEMA["additionalProperties"] is False
        assert RESPONSE_SCHEMA["properties"]["passed"] == {"type": "boolean"}
        assert RESPONSE_SCHEMA["properties"]["mentor_voice"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]
        assert remark["required"] == ["id", "what", "why", "todo"]
        assert remark["additionalProperties"] is False


class TestTheRequest:
    async def test_it_carries_the_strict_schema_and_expects_json(self) -> None:
        router = _Router(_answer())

        explained = await ReviewExplainerAgent(router).explain(  # type: ignore[arg-type]
            _SHOWN, execution=_execution()
        )

        (request,) = router.requests
        assert request["response_schema"] == RESPONSE_SCHEMA
        assert request["expects_json"] is True
        assert [r.id for r in explained.answer.remarks] == ["c1", "c2"]
        assert (explained.provider, explained.model) == ("dashscope", "qwen3.7-max")

    def test_the_model_is_shown_the_result_and_every_verdict(self) -> None:
        context = render_context(_SHOWN)

        assert context["result"] == {"passed": False, "score": 16}
        assert context["language"] == "Ukrainian"
        c1, c2, c3 = context["criteria"]
        assert c1 == {
            "id": "c1",
            "weight": "must",
            "text": "Criterion c1",
            "evidence": "What shows c1",
            "verdict": "not_met",
            "missing": None,
            "quote_not_found": True,
        }
        assert c2["verdict"] == "not_met"
        assert c2["points"] == [
            {
                "id": "c2.p1",
                "text": "Point 1 of c2",
                "verdict": "met",
                "quote": "return fibonacci(n - 1)",
                "place": {"file": "solution.py", "lines": [4, 4]},
                "kept_from_earlier": False,
            },
            {
                "id": "c2.p2",
                "text": "Point 2 of c2",
                "verdict": "not_met",
                "missing": "Файл tests.py не прочитано.",
                "quote_not_found": False,
            },
        ]
        # A document is pointed to by its file alone.
        assert c3["place"] == {"file": "README.docx", "lines": None}
        assert c3["kept_from_earlier"] is True

    def test_a_not_met_shows_no_quote_and_a_met_no_sentence(self) -> None:
        for criterion in render_context(_SHOWN)["criteria"]:
            for item in criterion.get("points", [criterion]):
                if item["verdict"] == "met":
                    assert "missing" not in item
                else:
                    assert "quote" not in item
                    assert "place" not in item


class TestTheCheckHolds:
    """Lock 7 at the agent: the validator the router runs is the check."""

    @pytest.mark.parametrize(
        ("content", "fault"),
        [
            pytest.param(_answer(passed=True), "passed must be false", id="passed"),
            pytest.param(
                _answer(remarks=("c2",)), "missing ['c1']", id="an-unmet-must-left-out"
            ),
        ],
    )
    async def test_an_answer_against_the_facts_is_refused(
        self, content: str, fault: str
    ) -> None:
        router = _Router(content)

        with pytest.raises(StructuralRetryError, match=fault.replace("[", r"\[")):
            await ReviewExplainerAgent(router).explain(  # type: ignore[arg-type]
                _SHOWN, execution=_execution()
            )


def _provider(*responses: LLMResponse) -> Any:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(responses))
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, provider="p", model_id="m")


def _real_router(qwen: Any, deepseek: Any) -> StageRouter:
    """The real router over the real registry; only the two providers are fakes."""
    return StageRouter(
        LadderConfig(),
        {"dashscope": qwen, "deepseek": deepseek},
        registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
        prompt_base_path=_REPO_ROOT,
    )


class TestThroughTheRealRouter:
    async def test_a_wrong_pass_is_retried_on_the_same_rung_with_the_reason(
        self,
    ) -> None:
        qwen = _provider(_response(_answer(passed=True)), _response(_answer()))
        deepseek = _provider()

        explained = await ReviewExplainerAgent(_real_router(qwen, deepseek)).explain(
            _SHOWN, execution=_execution()
        )

        assert qwen.complete.await_count == 2
        deepseek.complete.assert_not_awaited()
        retry: LLMRequest = qwen.complete.await_args_list[1].args[0]
        assert "passed must be false" in retry.prompt
        assert explained.answer.passed is False

    async def test_a_rung_that_keeps_disagreeing_gives_way_to_the_next(self) -> None:
        qwen = _provider(
            _response(_answer(remarks=("c2",))), _response(_answer(remarks=("c2",)))
        )
        deepseek = _provider(_response(_answer()))

        explained = await ReviewExplainerAgent(_real_router(qwen, deepseek)).explain(
            _SHOWN, execution=_execution()
        )

        assert qwen.complete.await_count == 2
        assert deepseek.complete.await_count == 1
        assert (explained.provider, explained.model) == ("deepseek", "deepseek-flash")

    async def test_no_agreeing_answer_on_any_rung_is_no_explanation(self) -> None:
        wrong = _response(_answer(passed=True))
        qwen = _provider(wrong, wrong)
        deepseek = _provider(wrong, wrong)

        with pytest.raises(LadderExhaustedError):
            await ReviewExplainerAgent(_real_router(qwen, deepseek)).explain(
                _SHOWN, execution=_execution()
            )

    async def test_the_schema_reaches_each_rung_in_its_own_form(self) -> None:
        """qwen3.7-max holds a strict schema; deepseek-flash gets JSON mode."""
        qwen = _provider(
            _response(_answer(passed=True)), _response(_answer(passed=True))
        )
        deepseek = _provider(_response(_answer()))

        await ReviewExplainerAgent(_real_router(qwen, deepseek)).explain(
            _SHOWN, execution=_execution()
        )

        sent_to_qwen: LLMRequest = qwen.complete.await_args_list[0].args[0]
        sent_to_deepseek: LLMRequest = deepseek.complete.await_args.args[0]
        assert sent_to_qwen.schema_mode is SchemaMode.STRICT
        assert sent_to_qwen.response_schema == RESPONSE_SCHEMA
        assert sent_to_deepseek.schema_mode is SchemaMode.JSON
        assert mentions_json(sent_to_deepseek)


def _system(shown: ExplanationInput = _SHOWN) -> str:
    """The rendered system prompt, its whitespace folded to single spaces."""
    rendered = load_prompt(_PROMPT_REF, base_path=_REPO_ROOT).render(
        **render_context(shown)
    )
    return " ".join((rendered.system or "").split())


_LANGUAGES = pytest.mark.parametrize("language", ["Ukrainian", "English"])


class TestThePromptSaysJson:
    """JSON mode on DeepSeek refuses a request whose messages lack the word."""

    @_LANGUAGES
    def test_every_rendering_mentions_json(self, language: str) -> None:
        rendered = load_prompt(_PROMPT_REF, base_path=_REPO_ROOT).render(
            **render_context(replace(_SHOWN, language=language))
        )
        request = LLMRequest(
            prompt=rendered.user or "",
            system_prompt=rendered.system,
            expects_json=True,
            response_schema=RESPONSE_SCHEMA,
            schema_mode=SchemaMode.JSON,
        )

        assert mentions_json(request)


class TestThePromptRules:
    """The rules of the session that opened K5 (2026-10-02), word for word."""

    _NO_LINKS = (
        "Do not name or point to lessons, materials, pages or links of the "
        "course, and write no URL or web address at all"
    )
    _QUOTE_NOT_FOUND = (
        "`quote_not_found: true` means that a confirmation of this item could "
        "not be found in the work. Say exactly that — never that the student "
        "did not do it."
    )
    _UNREAD_FILE = (
        "the cause is a limit of the system, not a fault of the student: say "
        "that this file could not be read, so this part could not be checked — "
        "never that the student did not do the work."
    )
    _CONTRAST = (
        "`what` — how it is done in the work now, or that it is not there; "
        "- `why` — what the problem is, and why it matters; - `todo` — how it "
        "should be done, and why that solves the problem."
    )
    # Vision-side, 2026-10-02, before the commit: the student sees which
    # remarks decide the pass, and never the weights' own names.
    _WEIGHT = (
        "Each remark makes clear, in plain words, how much it weighs: a "
        "criterion of weight `must` that is not met decides the pass; one of "
        "weight `should` or `may` makes the work better but does not decide the "
        "pass. Say it in plain words, never by the names of the weights "
        "(`must`, `should`, `may`)."
    )
    _ONE_REMARK_EACH = (
        "exactly one remark for every criterion whose verdict is `not_met`, "
        "whatever its weight, and none for a criterion that is met."
    )
    _PRAISE_IN_WHY = (
        "`why` — the voice of the course: why the work is passed or not, in "
        "terms of what the author required. Start with the essence, then the "
        "specifics. Name what is done well, and where the student went beyond "
        "the assignment"
    )
    _MENTOR_VOICE = (
        "`mentor_voice` — your own professional judgement as a senior "
        "colleague, only where it differs from the course or goes well beyond "
        "it. It never changes a verdict, the score or the pass"
    )
    _EVIDENCE = (
        "use whatever reads easiest: a short quote, a pointer — the file and "
        "the lines given in `place` — or a plain mention."
    )
    _NO_INTERNAL_TERMS = (
        "No internal terms: never write an identifier (`c3`, `c3.p2`), a field "
        "name or the words `met`, `not_met`, `quote_not_found`, "
        "`kept_from_earlier` in a text."
    )

    @_LANGUAGES
    @pytest.mark.parametrize(
        "rule",
        [
            "_NO_LINKS",
            "_QUOTE_NOT_FOUND",
            "_UNREAD_FILE",
            "_CONTRAST",
            "_WEIGHT",
            "_ONE_REMARK_EACH",
            "_PRAISE_IN_WHY",
            "_MENTOR_VOICE",
            "_EVIDENCE",
            "_NO_INTERNAL_TERMS",
        ],
    )
    def test_every_rendering_carries_the_rule(self, language: str, rule: str) -> None:
        system = _system(replace(_SHOWN, language=language))

        assert getattr(self, rule) in system

    @_LANGUAGES
    def test_the_language_is_the_students(self, language: str) -> None:
        system = _system(replace(_SHOWN, language=language))

        assert f"Write every text in **{language}**" in system
