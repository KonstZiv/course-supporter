"""Task 09a: the response schema on each connector's wire.

Locks:

* (1) schema + ``STRICT`` -> the schema itself, in each vendor's form
  (OpenAI-compatible ``json_schema``, Gemini ``response_json_schema``,
  DashScope ``response_format`` on both task-groups);
* (2) schema + ``JSON`` -> the vendor's JSON mode and no schema
  (``json_object`` for deepseek / deepseek_thinking / mistral);
* (3) no schema mode -> every connector's call kwargs exactly as before;
* the word "json": JSON mode on an OpenAI-compatible connector without it in
  the messages is refused before any call.
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.llm.providers.base import RequestConfigError
from course_supporter.llm.providers.dashscope import DashScopeProvider
from course_supporter.llm.providers.deepseek import DeepSeekProvider
from course_supporter.llm.providers.deepseek_thinking import (
    DeepSeekThinkingProvider,
)
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest, SchemaMode

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["pass", "fail"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _request(
    mode: SchemaMode | None,
    *,
    schema: dict[str, Any] | None = _SCHEMA,
    expects_json: bool = True,
    prompt: str = "Answer in JSON.",
    contents: list[Any] | None = None,
) -> LLMRequest:
    return LLMRequest(
        prompt=prompt,
        system_prompt="sys",
        model="m",
        max_tokens=256,
        action="safety_check",
        expects_json=expects_json,
        response_schema=schema,
        schema_mode=mode,
        contents=contents,
    )


# ── OpenAI-compatible: openai, mistral, deepseek, deepseek_thinking ──


def _openai_compat(kind: str) -> OpenAICompatProvider:
    if kind == "deepseek":
        return DeepSeekProvider(api_keys=("k",), default_model="m")
    if kind == "deepseek_thinking":
        return DeepSeekThinkingProvider(api_keys=("k",), default_model="m")
    return OpenAICompatProvider(api_keys=("k",), default_model="m", provider_name=kind)


async def _openai_kwargs(provider: OpenAICompatProvider, request: LLMRequest) -> Any:
    completion = MagicMock()
    choice = MagicMock()
    choice.message.content = '{"verdict": "pass"}'
    choice.finish_reason = "stop"
    completion.choices = [choice]
    completion.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=completion)
    provider._client_cycle = itertools.cycle([client])
    provider.check_request(request)
    await provider.complete(request)
    return client.chat.completions.create.await_args.kwargs


_OPENAI_KINDS = ["openai", "mistral", "deepseek", "deepseek_thinking"]
_VENDOR_HOOK = {
    "openai": {},
    "mistral": {},
    "deepseek": {"extra_body": {"thinking": {"type": "disabled"}}},
    "deepseek_thinking": {},
}


class TestOpenAICompatWire:
    @pytest.mark.parametrize("kind", _OPENAI_KINDS)
    async def test_strict_sends_the_schema(self, kind: str) -> None:
        kwargs = await _openai_kwargs(_openai_compat(kind), _request(SchemaMode.STRICT))
        assert kwargs["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "safety_check", "strict": True, "schema": _SCHEMA},
        }

    @pytest.mark.parametrize("kind", _OPENAI_KINDS)
    async def test_json_mode_sends_json_object_and_no_schema(self, kind: str) -> None:
        kwargs = await _openai_kwargs(_openai_compat(kind), _request(SchemaMode.JSON))
        assert kwargs["response_format"] == {"type": "json_object"}
        assert "json_schema" not in repr(kwargs)

    @pytest.mark.parametrize("kind", _OPENAI_KINDS)
    @pytest.mark.parametrize("expects_json", [False, True])
    async def test_no_schema_keeps_todays_kwargs(
        self, kind: str, expects_json: bool
    ) -> None:
        request = _request(None, schema=None, expects_json=expects_json)
        kwargs = await _openai_kwargs(_openai_compat(kind), request)
        assert kwargs == {
            "model": "m",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "Answer in JSON."},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
            **_VENDOR_HOOK[kind],
        }

    @pytest.mark.parametrize("kind", _OPENAI_KINDS)
    async def test_schema_without_mode_keeps_todays_kwargs(self, kind: str) -> None:
        with_schema = await _openai_kwargs(_openai_compat(kind), _request(None))
        no_schema = _request(None, schema=None)
        without = await _openai_kwargs(_openai_compat(kind), no_schema)
        assert with_schema == without

    async def test_vendor_hook_stays_its_own(self) -> None:
        # The deepseek thinking switch is not merged with the schema kwarg.
        provider = _openai_compat("deepseek")
        kwargs = await _openai_kwargs(provider, _request(SchemaMode.JSON))
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


class TestJsonWord:
    @pytest.mark.parametrize("kind", _OPENAI_KINDS)
    async def test_json_mode_without_the_word_is_refused_before_any_call(
        self, kind: str
    ) -> None:
        provider = _openai_compat(kind)
        request = _request(SchemaMode.JSON, prompt="Answer briefly.")
        with pytest.raises(RequestConfigError, match="json"):
            provider.check_request(request)

    @pytest.mark.parametrize("text", ["json", "JSON", "Return a Json object"])
    def test_any_case_in_either_message_passes(self, text: str) -> None:
        provider = _openai_compat("deepseek")
        provider.check_request(_request(SchemaMode.JSON, prompt=text))
        in_system = _request(SchemaMode.JSON, prompt="no word").model_copy(
            update={"system_prompt": text}
        )
        provider.check_request(in_system)

    @pytest.mark.parametrize(
        "mode", [SchemaMode.STRICT, None], ids=["strict", "no-mode"]
    )
    def test_only_json_mode_needs_the_word(self, mode: SchemaMode | None) -> None:
        _openai_compat("deepseek").check_request(_request(mode, prompt="no word"))

    def test_other_connectors_do_not_check(self) -> None:
        request = _request(SchemaMode.JSON, prompt="no word")
        _gemini().check_request(request)
        DashScopeProvider(api_keys=("k",), default_model="m").check_request(request)


# ── Gemini ──


def _gemini() -> GeminiProvider:
    with patch("course_supporter.llm.providers.gemini.genai.Client"):
        return GeminiProvider(api_keys=("k",), default_model="m")


async def _gemini_config(request: LLMRequest) -> Any:
    provider = _gemini()
    response = MagicMock()
    response.text = '{"verdict": "pass"}'
    response.candidates = []
    response.usage_metadata = MagicMock(
        prompt_token_count=1, candidates_token_count=1, thoughts_token_count=None
    )
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=response)
    provider._client_cycle = itertools.cycle([client])
    await provider.complete(request)
    return client.aio.models.generate_content.await_args.kwargs["config"]


class TestGeminiWire:
    async def test_strict_sends_response_json_schema(self) -> None:
        config = await _gemini_config(_request(SchemaMode.STRICT))
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == _SCHEMA
        assert config.response_schema is None

    async def test_json_mode_is_the_mime_type_alone(self) -> None:
        config = await _gemini_config(_request(SchemaMode.JSON))
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema is None
        assert config.response_schema is None

    @pytest.mark.parametrize("expects_json", [False, True])
    async def test_no_schema_keeps_todays_config(self, expects_json: bool) -> None:
        config = await _gemini_config(
            _request(None, schema=None, expects_json=expects_json)
        )
        assert config.model_dump(exclude_none=True) == {
            "temperature": 0.0,
            "max_output_tokens": 256,
            "system_instruction": "sys",
            **({"response_mime_type": "application/json"} if expects_json else {}),
        }

    async def test_schema_without_mode_keeps_todays_config(self) -> None:
        with_schema = await _gemini_config(_request(None))
        without = await _gemini_config(_request(None, schema=None))
        assert with_schema == without


# ── DashScope: text and multimodal task-groups ──


async def _dashscope_kwargs(
    request: LLMRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    from course_supporter.llm.providers import dashscope as ds_module

    response = MagicMock()
    response.status_code = 200
    response.output = {"text": '{"verdict": "pass"}'}
    response.usage = {"input_tokens": 1, "output_tokens": 1}
    fake = AsyncMock(return_value=response)
    target = (
        ds_module.AioMultiModalConversation
        if request.contents
        else ds_module.AioGeneration
    )
    monkeypatch.setattr(target, "call", fake)
    await DashScopeProvider(api_keys=("k",), default_model="m").complete(request)
    kwargs: dict[str, Any] = fake.await_args.kwargs
    return kwargs


_BRANCHES = pytest.mark.parametrize(
    "contents", [None, [_PNG]], ids=["text", "multimodal"]
)


class TestDashScopeWire:
    @_BRANCHES
    async def test_strict_sends_the_schema(
        self, contents: list[Any] | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs = await _dashscope_kwargs(
            _request(SchemaMode.STRICT, contents=contents), monkeypatch
        )
        assert kwargs["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "safety_check", "strict": True, "schema": _SCHEMA},
        }

    @_BRANCHES
    async def test_json_mode_sends_json_object(
        self, contents: list[Any] | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs = await _dashscope_kwargs(
            _request(SchemaMode.JSON, contents=contents), monkeypatch
        )
        assert kwargs["response_format"] == {"type": "json_object"}

    @_BRANCHES
    @pytest.mark.parametrize("schema", [None, _SCHEMA], ids=["no-schema", "no-mode"])
    async def test_no_mode_keeps_todays_kwargs(
        self,
        contents: list[Any] | None,
        schema: dict[str, Any] | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kwargs = await _dashscope_kwargs(
            _request(None, schema=schema, contents=contents), monkeypatch
        )
        assert set(kwargs) == {
            "model",
            "api_key",
            "temperature",
            "max_tokens",
            "messages",
        }


class TestRequestContract:
    def test_schema_requires_expects_json(self) -> None:
        with pytest.raises(ValueError, match="expects_json"):
            _request(None, expects_json=False)

    def test_mode_requires_a_schema(self) -> None:
        with pytest.raises(ValueError, match="response_schema"):
            _request(SchemaMode.JSON, schema=None)
