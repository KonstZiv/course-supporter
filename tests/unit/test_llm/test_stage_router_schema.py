"""Task 09a: the router's half of the response schema.

* The stage hands over a schema; the router picks the wire mode per rung from
  the model's registry capabilities (``schema_strict`` -> strict, ``json_mode``
  -> JSON, neither -> no mode).
* A request the rung's connector refuses (``check_request``) stops the stage
  before the call: nothing is paid, nothing descends.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from course_supporter.llm.error_categories import ErrorCategory
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
