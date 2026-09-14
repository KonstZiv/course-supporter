"""Finish-reason normalisation — the shared mapper and all four connector modules.

Each connector test feeds a response built from the vendor SDK's own types
(not a MagicMock), so the attribute path the connector reads is the one the
SDK really exposes: a mock would answer any attribute and prove nothing about
where the vendor puts the reason (impl-rules#13).
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest
from dashscope.api_entities.dashscope_response import (
    Choice as DashScopeChoice,
)
from dashscope.api_entities.dashscope_response import (
    GenerationOutput,
    GenerationResponse,
    GenerationUsage,
)
from dashscope.api_entities.dashscope_response import (
    Message as DashScopeMessage,
)
from google.genai import types as genai_types
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from course_supporter.llm.finish_reason import FinishReason, normalize_finish_reason
from course_supporter.llm.providers.anthropic import AnthropicProvider
from course_supporter.llm.providers.dashscope import DashScopeProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest

_REQUEST = LLMRequest(prompt="hi", model="m", max_tokens=8192)


class TestNormalizeFinishReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("length", FinishReason.OUTPUT_CEILING),
            ("stop", FinishReason.STOP),
            ("content_filter", FinishReason.OTHER),
            (None, FinishReason.UNKNOWN),
            ("", FinishReason.UNKNOWN),
            ("unset", FinishReason.UNKNOWN),
        ],
    )
    def test_maps_vendor_values(self, raw: object, expected: FinishReason) -> None:
        result = normalize_finish_reason(
            raw, ceiling={"length"}, stop={"stop"}, unreported={"unset"}
        )
        assert result is expected

    def test_unlisted_value_is_never_a_clean_stop(self) -> None:
        assert (
            normalize_finish_reason("brand_new", ceiling=set(), stop=set())
            is FinishReason.OTHER
        )


# ── OpenAI-compatible (OpenAI / DeepSeek / Mistral) ────────────────────


def _chat_completion(finish_reason: str | None, content: str | None) -> Any:
    choice = {
        "index": 0,
        "finish_reason": finish_reason,
        "message": {"role": "assistant", "content": content},
    }
    body = {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [choice],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 1},
    }
    if finish_reason is None:
        # The SDK type does not admit a missing reason; a vendor that omits it
        # still reaches the connector, so build around validation for this row.
        unreported = Choice.model_construct(
            index=0,
            finish_reason=None,
            message=ChatCompletionMessage(role="assistant", content=content),
        )
        return ChatCompletion.model_construct(**{**body, "choices": [unreported]})
    return ChatCompletion.model_validate(body)


class TestOpenAICompatFinishReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("length", FinishReason.OUTPUT_CEILING),
            ("stop", FinishReason.STOP),
            ("content_filter", FinishReason.OTHER),
            (None, FinishReason.UNKNOWN),
        ],
    )
    async def test_complete_normalises_choice_finish_reason(
        self, raw: str | None, expected: FinishReason
    ) -> None:
        provider = OpenAICompatProvider(api_keys=("k",), default_model="m")
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            return_value=_chat_completion(raw, content="" if raw else None)
        )
        provider._client_cycle = itertools.cycle([client])

        response = await provider.complete(_REQUEST)

        assert response.finish_reason is expected

    async def test_reasoning_spent_the_ceiling_reads_as_ceiling_with_empty_body(
        self,
    ) -> None:
        """The step-E shape: thinking consumed max_tokens, content is null."""
        provider = OpenAICompatProvider(api_keys=("k",), default_model="m")
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            return_value=_chat_completion("length", content=None)
        )
        provider._client_cycle = itertools.cycle([client])

        response = await provider.complete(_REQUEST)

        assert response.content == ""
        assert response.finish_reason is FinishReason.OUTPUT_CEILING


# ── DashScope ───────────────────────────────────────────────────────────


def _dashscope_text_response(
    *, flat: str | None = None, in_choice: str | None = None
) -> GenerationResponse:
    output = GenerationOutput(
        text=None,
        finish_reason=flat,
        choices=[
            DashScopeChoice(
                finish_reason=in_choice,
                message=DashScopeMessage(role="assistant", content=""),
            )
        ],
    )
    return GenerationResponse(
        status_code=200,
        request_id="r",
        output=output,
        usage=GenerationUsage(input_tokens=10, output_tokens=8192),
    )


class TestDashScopeFinishReason:
    @pytest.mark.parametrize(
        ("flat", "in_choice", "expected"),
        [
            (None, "length", FinishReason.OUTPUT_CEILING),
            (None, "stop", FinishReason.STOP),
            ("length", None, FinishReason.OUTPUT_CEILING),
            ("stop", None, FinishReason.STOP),
            (None, "tool_calls", FinishReason.OTHER),
            (None, "null", FinishReason.UNKNOWN),
            (None, None, FinishReason.UNKNOWN),
        ],
        ids=[
            "choice-length",
            "choice-stop",
            "flat-length",
            "flat-stop",
            "choice-other",
            "null-placeholder",
            "absent",
        ],
    )
    async def test_complete_normalises_either_output_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flat: str | None,
        in_choice: str | None,
        expected: FinishReason,
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = DashScopeProvider(api_keys=("k",), default_model="m", base_url=None)
        monkeypatch.setattr(
            ds_module.AioGeneration,
            "call",
            AsyncMock(
                return_value=_dashscope_text_response(flat=flat, in_choice=in_choice)
            ),
        )

        response = await provider.complete(_REQUEST)

        assert response.finish_reason is expected


# ── Gemini ──────────────────────────────────────────────────────────────


def _gemini_response(
    finish_reason: genai_types.FinishReason | None, *, candidates: bool = True
) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        candidates=(
            [genai_types.Candidate(finish_reason=finish_reason)] if candidates else []
        ),
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=0
        ),
    )


class TestGeminiFinishReason:
    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (
                _gemini_response(genai_types.FinishReason.MAX_TOKENS),
                FinishReason.OUTPUT_CEILING,
            ),
            (_gemini_response(genai_types.FinishReason.STOP), FinishReason.STOP),
            (_gemini_response(genai_types.FinishReason.SAFETY), FinishReason.OTHER),
            (
                _gemini_response(genai_types.FinishReason.FINISH_REASON_UNSPECIFIED),
                FinishReason.UNKNOWN,
            ),
            (_gemini_response(None), FinishReason.UNKNOWN),
            (_gemini_response(None, candidates=False), FinishReason.UNKNOWN),
        ],
        ids=["max-tokens", "stop", "safety", "unspecified", "absent", "no-candidate"],
    )
    async def test_complete_normalises_candidate_finish_reason(
        self,
        response: genai_types.GenerateContentResponse,
        expected: FinishReason,
    ) -> None:
        provider = GeminiProvider(api_keys=("k",), default_model="m")
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        provider._client_cycle = itertools.cycle([client])

        result = await provider.complete(_REQUEST)

        assert result.finish_reason is expected


# ── Anthropic ───────────────────────────────────────────────────────────


def _anthropic_message(stop_reason: str | None) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "content": [],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 8192},
        }
    )


class TestAnthropicFinishReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("max_tokens", FinishReason.OUTPUT_CEILING),
            ("end_turn", FinishReason.STOP),
            ("stop_sequence", FinishReason.STOP),
            ("refusal", FinishReason.OTHER),
            (None, FinishReason.UNKNOWN),
        ],
    )
    async def test_complete_normalises_stop_reason(
        self, raw: str | None, expected: FinishReason
    ) -> None:
        provider = AnthropicProvider(api_keys=("k",), default_model="m")
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=_anthropic_message(raw))
        provider._client_cycle = itertools.cycle([client])

        response = await provider.complete(_REQUEST)

        assert response.finish_reason is expected
