"""The measurement of the explanation prompts, on a recorded input, offline.

Lock e of the order on prompt v3: the measuring script renders v2 and v3 from
a recorded input without the network. The recorded input is
``tests/fixtures/review_explanation/recorded_input.json`` — synthetic, in the
format ``scripts/record_explanation_input.py`` writes. Here:

* the record reads back to the agent's input and writes out to the same JSON;
* each prompt is rendered by the real router for the stage's first rung,
  pinned and limited as the path runs it, and nothing is sent: the system
  messages are v2's and v3's own, the user message one and the same;
* one call per prompt over a provider double — no retry even on an answer
  the stage would refuse — priced by the registry and checked by the facts;
* the summary and the answers land in the output folder;
* the script's dry run prints the same, with no key and no call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scripts import measure_explanation_prompt

from course_supporter.agents.review_explainer import RESPONSE_SCHEMA, render_context
from course_supporter.homework import explanation_measure as measure
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.homework.path_stages import _execution
from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import ModelRegistryConfig, load_registry
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECORD = (
    _REPO_ROOT / "tests" / "fixtures" / "review_explanation" / "recorded_input.json"
)


@pytest.fixture(scope="module")
def record() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_RECORD.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def stage() -> ModelStage:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    found = config.stages[measure.STAGE_NAME]
    assert isinstance(found, ModelStage)
    return found


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    return load_registry(_REPO_ROOT / "config" / "external_services.yaml")


class _Provider(LLMProvider):
    """A provider double: answers in turn, counts what it is asked."""

    def __init__(self, *responses: LLMResponse | Exception) -> None:
        super().__init__()
        self.provider_name = "dashscope"
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def classify_error(self, exc: Exception) -> ErrorCategory:
        return ErrorCategory.INFRASTRUCTURE


def _answer(*, passed: bool, remarks: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "passed": passed,
            "why": "Функцію написано чисто; тестів поки замало.",
            "remarks": [
                {
                    "id": cid,
                    "what": "Зараз цього в роботі немає.",
                    "why": "Без цього розв'язок не перевірено.",
                    "todo": "Додайте перевірку у файлі тестів.",
                    "links": [],
                }
                for cid in remarks
            ],
            "mentor_voice": None,
        },
        ensure_ascii=False,
    )


def _response(content: str, **usage: Any) -> LLMResponse:
    return LLMResponse(
        content=content, provider="dashscope", model_id="qwen3.7-max", **usage
    )


class TestTheRecord:
    def test_it_reads_back_to_the_agents_input_and_out_again(
        self, record: dict[str, Any]
    ) -> None:
        shown = measure.input_of(record)

        assert shown.facts.unmet == ("c2", "c3", "c4")
        assert shown.facts.passed is False
        assert (
            measure.record_of(
                shown,
                submission_id=record["submission_id"],
                provenance=record["provenance"],
            )
            == record
        )

    def test_another_format_is_refused(self, record: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="not a recorded explanation input"):
            measure.input_of({**record, "format": "something/else"})


class TestTheFirstRung:
    def test_it_is_the_paths_own_cut_to_its_first_rung(self, stage: ModelStage) -> None:
        """The translation agrees with the path's (``path_stages._execution``)."""
        context = SimpleNamespace(stage=stage, stage_name=measure.STAGE_NAME)
        path = _execution(context)  # type: ignore[arg-type]

        own = measure.first_rung(stage, path.stage.prompt_ref)

        assert own.ladder == path.stage.ladder[:1]
        assert own.model_dump(exclude={"ladder"}) == path.stage.model_dump(
            exclude={"ladder"}
        )


class TestRenderedWithoutTheNetwork:
    """Lock e: both prompts, rendered from the record, nothing sent."""

    async def test_each_prompt_renders_its_own_system_and_the_same_user(
        self, record: dict[str, Any], stage: ModelStage, registry: ModelRegistryConfig
    ) -> None:
        shown = measure.input_of(record)
        context = render_context(shown)

        sent = [
            await measure.send_once(
                shown,
                ref,
                stage=stage,
                registry=registry,
                provider=None,
                prompt_base_path=_REPO_ROOT,
            )
            for ref in measure.PROMPT_REFS
        ]

        for item in sent:
            assert item.request is not None
            assert item.response is None  # captured, never sent
            rendered = load_prompt(item.prompt_ref, base_path=_REPO_ROOT).render(
                **context
            )
            assert item.request.system_prompt == rendered.system
            assert item.request.prompt == rendered.user
            # As the stage sends it: the first rung, its pin, the schema.
            assert (item.provider, item.request.model) == ("dashscope", "qwen3.7-max")
            assert item.request.max_tokens == 8192
            assert item.request.expects_json is True
            assert item.request.response_schema == RESPONSE_SCHEMA
            assert item.request.schema_mode is SchemaMode.STRICT
        v2, v3 = sent
        assert measure.same_user_message(sent)
        assert v2.request is not None
        assert v3.request is not None
        assert v2.request.system_prompt != v3.request.system_prompt
        assert "**Ukrainian**" in (v3.request.system_prompt or "")
        assert v2.request_hash != v3.request_hash
        # The work and the course links went in whole.
        assert shown.submission_text in v3.request.prompt
        assert "Тестування з pytest" in v3.request.prompt


class TestOneCallPerPrompt:
    async def _measured(
        self,
        record: dict[str, Any],
        stage: ModelStage,
        registry: ModelRegistryConfig,
        provider: _Provider,
    ) -> measure.Measured:
        shown = measure.input_of(record)
        sent = await measure.send_once(
            shown,
            measure.PROMPT_REFS[1],
            stage=stage,
            registry=registry,
            provider=provider,
            prompt_base_path=_REPO_ROOT,
        )
        return measure.measured(sent, shown.facts, registry=registry)

    async def test_an_answer_is_priced_by_the_registry_and_checked(
        self, record: dict[str, Any], stage: ModelStage, registry: ModelRegistryConfig
    ) -> None:
        provider = _Provider(
            _response(
                _answer(passed=False, remarks=("c2", "c3", "c4")),
                tokens_in=10_000,
                tokens_out=1_000,
            )
        )

        result = await self._measured(record, stage, registry, provider)

        assert len(provider.requests) == 1
        assert result.check == "agrees"
        assert (result.label, result.tokens_in, result.tokens_out) == (
            "v3",
            10_000,
            1_000,
        )
        prices = registry.models["qwen3.7-max"].prices
        assert prices.in_per_1k is not None
        assert prices.out_per_1k is not None
        expected = (10_000 * prices.in_per_1k + 1_000 * prices.out_per_1k) / 1000
        assert result.cost_usd is not None
        assert float(result.cost_usd) == pytest.approx(float(expected), abs=1e-6)
        assert result.cost_kind == "exact"

    async def test_an_answer_against_the_facts_is_kept_and_not_retried(
        self, record: dict[str, Any], stage: ModelStage, registry: ModelRegistryConfig
    ) -> None:
        provider = _Provider(
            _response(_answer(passed=True, remarks=("c2", "c3", "c4")))
        )

        result = await self._measured(record, stage, registry, provider)

        assert len(provider.requests) == 1
        assert result.check.startswith("refused: ")
        assert result.content is not None

    async def test_a_failed_call_is_one_call_and_says_why(
        self, record: dict[str, Any], stage: ModelStage, registry: ModelRegistryConfig
    ) -> None:
        provider = _Provider(TimeoutError("timed out"))

        result = await self._measured(record, stage, registry, provider)

        assert len(provider.requests) == 1
        assert result.content is None
        assert result.check == "no answer"
        assert result.failure is not None


class TestTheOutput:
    def test_the_answers_and_the_summary_are_written(
        self, tmp_path: Path, record: dict[str, Any]
    ) -> None:
        def result(label: str, content: str | None, cost: str | None) -> Any:
            return measure.Measured(
                label=label,
                prompt_ref=f"prompts/review_explanation/{label}.md",
                provider="dashscope",
                model="qwen3.7-max",
                request_hash="h",
                content=content,
                failure=None,
                tokens_in=1,
                tokens_in_cached=None,
                tokens_out=1,
                tokens_reasoning=None,
                finish_reason="stop",
                latency_ms=1,
                cost_usd=None if cost is None else Decimal(cost),
                cost_kind="exact",
                tariff_window="flat",
                check="agrees",
            )

        summary = measure.write_measurement(
            tmp_path,
            [result("v2", '{"a": "б"}', "0.01"), result("v3", "not json", "0.02")],
            record=record,
            same_user=True,
        )

        assert json.loads((tmp_path / "v2.answer.json").read_text("utf-8")) == {
            "a": "б"
        }
        assert (tmp_path / "v3.answer.txt").read_text("utf-8") == "not json"
        written = json.loads(summary.read_text("utf-8"))
        assert written["format"] == measure.SUMMARY_FORMAT
        assert written["user_message_identical"] is True
        assert written["cost_usd_total"] == "0.03"
        assert [c["label"] for c in written["calls"]] == ["v2", "v3"]
        assert written["provenance"] == record["provenance"]


class TestTheScriptsDryRun:
    async def test_it_calls_nothing_and_says_the_user_messages_are_the_same(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_providers(*_args: Any) -> None:
            raise AssertionError("a dry run builds no provider")

        monkeypatch.setattr(
            measure_explanation_prompt, "create_providers", no_providers
        )
        monkeypatch.chdir(_REPO_ROOT)
        args = measure_explanation_prompt._arguments([str(_RECORD), "--dry-run"])

        code = await measure_explanation_prompt._main(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "user messages identical: True" in out
        for ref in measure.PROMPT_REFS:
            assert f"{ref}: dashscope/qwen3.7-max" in out
