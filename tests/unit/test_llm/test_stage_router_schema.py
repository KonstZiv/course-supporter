"""Task 09a: the router's half of the response schema.

* The stage hands over a schema; the router picks the wire mode per rung from
  the model's registry capabilities (``schema_strict`` -> strict, ``json_mode``
  -> JSON, neither -> no mode).
* A request the rung's connector refuses (``check_request``) stops the stage
  before the call: nothing is paid, nothing descends.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.call_outcome import CallOutcome
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    LadderStop,
    StructuralRetryError,
)
from course_supporter.llm.finish_reason import FinishReason
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers.base import LLMProvider, RequestConfigError
from course_supporter.llm.registry import (
    Capability,
    ModelRegistryConfig,
    ProviderConfig,
    ProviderModelConfig,
)
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode
from course_supporter.llm.stage_router import StageExecution, StageRouter

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _registry(**capabilities: list[Capability]) -> ModelRegistryConfig:
    return ModelRegistryConfig(
        providers={
            "p": ProviderConfig(
                type="llm",
                models=[
                    ProviderModelConfig(id=model, capabilities=caps)
                    for model, caps in capabilities.items()
                ],
            )
        },
        actions={},
    )


def _stage(model: str = "m") -> StageConfig:
    return StageConfig(
        prompt_ref="prompts/example/v1.md",
        ladder=[LadderEntry(provider="p", model=model)],
    )


def _provider(content: str = '{"ok": true}') -> Any:
    p = AsyncMock(spec=LLMProvider)
    p.enabled = True
    p.complete = AsyncMock(
        return_value=LLMResponse(content=content, provider="p", model_id="m")
    )
    p.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return p


@pytest.fixture(autouse=True)
def _prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "course_supporter.llm.stage_router.load_prompt",
        lambda prompt_ref, *, base_path=None: StagePrompt(
            system="Answer in JSON.", user="question"
        ),
    )


def _sent(provider: Any) -> LLMRequest:
    request: LLMRequest = provider.complete.await_args.args[0]
    return request


class TestModeFromCapabilities:
    @pytest.mark.parametrize(
        ("capabilities", "mode"),
        [
            ([Capability.JSON_MODE, Capability.SCHEMA_STRICT], SchemaMode.STRICT),
            ([Capability.JSON_MODE], SchemaMode.JSON),
            ([Capability.LONG_CONTEXT], None),
        ],
        ids=["schema_strict", "json_mode", "neither"],
    )
    async def test_mode_follows_the_rung_model(
        self, capabilities: list[Capability], mode: SchemaMode | None
    ) -> None:
        provider = _provider()
        router = StageRouter(
            LadderConfig(), {"p": provider}, registry=_registry(m=capabilities)
        )

        await router.execute_stage(
            _stage(), "s", expects_json=True, response_schema=_SCHEMA
        )

        sent = _sent(provider)
        assert sent.response_schema == _SCHEMA
        assert sent.schema_mode is mode

    async def test_model_outside_the_registry_gets_no_mode(self) -> None:
        provider = _provider()
        router = StageRouter(LadderConfig(), {"p": provider}, registry=_registry())

        await router.execute_stage(
            _stage(), "s", expects_json=True, response_schema=_SCHEMA
        )

        assert _sent(provider).schema_mode is None

    async def test_no_schema_no_mode(self) -> None:
        provider = _provider()
        router = StageRouter(
            LadderConfig(),
            {"p": provider},
            registry=_registry(m=[Capability.JSON_MODE, Capability.SCHEMA_STRICT]),
        )

        await router.execute_stage(_stage(), "s", expects_json=True)

        sent = _sent(provider)
        assert sent.response_schema is None
        assert sent.schema_mode is None

    async def test_by_name_entry_and_stage_execution_carry_the_schema(self) -> None:
        provider = _provider()
        registry = _registry(m=[Capability.JSON_MODE])
        router = StageRouter(
            LadderConfig(stages={"named": _stage()}), {"p": provider}, registry=registry
        )

        await router.execute_for_stage(
            "named", expects_json=True, response_schema=_SCHEMA
        )
        assert _sent(provider).schema_mode is SchemaMode.JSON

        await StageExecution(stage=_stage(), stage_name="path").run(
            router, expects_json=True, response_schema=_SCHEMA
        )
        assert _sent(provider).response_schema == _SCHEMA


class TestRefusedBeforeTheCall:
    async def test_connector_refusal_propagates_unpaid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows: list[dict[str, Any]] = []

        async def _persist(_factory: Any, **kwargs: Any) -> None:
            rows.append(kwargs)

        monkeypatch.setattr("course_supporter.llm.stage_router._persist", _persist)

        def _refuse(_request: LLMRequest) -> None:
            raise RequestConfigError("no json word")

        refusing = _provider()
        refusing.check_request = _refuse
        second = _provider()
        stage = StageConfig(
            prompt_ref="prompts/example/v1.md",
            ladder=[
                LadderEntry(provider="p", model="m"),
                LadderEntry(provider="q", model="m"),
            ],
        )
        router = StageRouter(
            LadderConfig(),
            {"p": refusing, "q": second},
            registry=_registry(m=[Capability.JSON_MODE]),
            session_factory=object(),  # type: ignore[arg-type]
        )

        with pytest.raises(RequestConfigError):
            await router.execute_stage(
                stage, "s", expects_json=True, response_schema=_SCHEMA
            )

        refusing.complete.assert_not_awaited()
        second.complete.assert_not_awaited()
        assert rows == []


# ── K3: a schema answer cut off at the output ceiling ──

_CUT = '{"ok": tr'


def _answers(*responses: tuple[str, FinishReason]) -> Any:
    p = _provider()
    p.complete = AsyncMock(
        side_effect=[
            LLMResponse(
                content=content,
                provider="p",
                model_id="m",
                tokens_in=10,
                tokens_out=16,
                finish_reason=finish,
            )
            for content, finish in responses
        ]
    )
    return p


def _two_rungs() -> StageConfig:
    return StageConfig(
        prompt_ref="prompts/example/v1.md",
        ladder=[
            LadderEntry(provider="p", model="m"),
            LadderEntry(provider="q", model="m"),
        ],
    )


def _strict_validator(seen: list[str]) -> Any:
    def _validate(content: str) -> None:
        seen.append(content)
        try:
            json.loads(content)
        except ValueError as exc:
            raise StructuralRetryError(f"not JSON: {exc}") from exc

    return _validate


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _persist(_factory: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("course_supporter.llm.stage_router._persist", _persist)
    return captured


def _router(first: Any, second: Any) -> StageRouter:
    return StageRouter(
        LadderConfig(stages={"named": _two_rungs()}),
        {"p": first, "q": second},
        registry=_registry(m=[Capability.JSON_MODE, Capability.SCHEMA_STRICT]),
        session_factory=object(),  # type: ignore[arg-type]
    )


class TestTruncatedUnderSchema:
    async def test_cut_answer_stops_the_path_stage_after_one_paid_attempt(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = _answers((_CUT, FinishReason.OUTPUT_CEILING))
        second = _provider()
        seen: list[str] = []

        with pytest.raises(LadderExhaustedError) as caught:
            await _router(first, second).execute_stage(
                _two_rungs(),
                "s",
                expects_json=True,
                response_schema=_SCHEMA,
                response_validator=_strict_validator(seen),
                stop_on_output_ceiling=True,
            )

        assert caught.value.stop is LadderStop.OUTPUT_CEILING
        first.complete.assert_awaited_once()  # no structural retry
        second.complete.assert_not_awaited()  # no descent
        assert seen == []  # the validator never saw the cut document
        (paid,) = [r for r in rows if r["outcome"] is not CallOutcome.ABANDONED]
        assert paid["outcome"] is CallOutcome.INVALID_CONTENT
        assert paid["finish_reason"] is FinishReason.OUTPUT_CEILING
        assert paid["error_message"] == "output ceiling: truncated response"

    async def test_cut_structural_retry_stops_too(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = _answers(
            ("prose, not JSON", FinishReason.STOP),
            (_CUT, FinishReason.OUTPUT_CEILING),
        )
        second = _provider()

        with pytest.raises(LadderExhaustedError) as caught:
            await _router(first, second).execute_stage(
                _two_rungs(),
                "s",
                expects_json=True,
                response_schema=_SCHEMA,
                response_validator=_strict_validator([]),
                stop_on_output_ceiling=True,
            )

        assert caught.value.stop is LadderStop.OUTPUT_CEILING
        assert first.complete.await_count == 2
        second.complete.assert_not_awaited()

    async def test_prose_under_schema_keeps_the_structural_retry(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = _answers(
            ("prose, not JSON", FinishReason.STOP),
            ('{"ok": true}', FinishReason.STOP),
        )
        seen: list[str] = []

        result = await _router(first, _provider()).execute_stage(
            _two_rungs(),
            "s",
            expects_json=True,
            response_schema=_SCHEMA,
            response_validator=_strict_validator(seen),
            stop_on_output_ceiling=True,
        )

        assert result.content == '{"ok": true}'
        assert seen == ["prose, not JSON", '{"ok": true}']
        assert rows[0]["outcome"] is CallOutcome.INVALID_CONTENT
        assert rows[0]["finish_reason"] is FinishReason.STOP

    async def test_without_schema_the_cut_body_goes_to_the_validator(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = _answers(
            (_CUT, FinishReason.OUTPUT_CEILING),
            ('{"ok": true}', FinishReason.STOP),
        )
        seen: list[str] = []

        result = await _router(first, _provider()).execute_stage(
            _two_rungs(),
            "s",
            expects_json=True,
            response_validator=_strict_validator(seen),
            stop_on_output_ceiling=True,
        )

        assert seen[0] == _CUT
        assert first.complete.await_count == 2  # today's structural retry
        assert result.content == '{"ok": true}'

    async def test_by_name_entry_keeps_validating_a_cut_schema_answer(
        self, rows: list[dict[str, Any]]
    ) -> None:
        first = _answers(
            (_CUT, FinishReason.OUTPUT_CEILING),
            (_CUT, FinishReason.OUTPUT_CEILING),
        )
        second = _provider()
        seen: list[str] = []

        result = await _router(first, second).execute_for_stage(
            "named",
            expects_json=True,
            response_schema=_SCHEMA,
            response_validator=_strict_validator(seen),
        )

        # Today's path: validator -> structural retry on the rung -> descent.
        assert seen[:2] == [_CUT, _CUT]
        assert first.complete.await_count == 2
        assert result.provider_used == "q"
