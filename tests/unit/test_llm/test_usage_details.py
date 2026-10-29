"""Every usage detail the connectors can see reaches ``LLMResponse`` (BIL-C).

An exact price needs the provider's own split of the call: input served from
cache (billed at the cached price) and reasoning (billed as output). Each test
feeds a connector the usage shape its provider returns and checks the split
arrives. Lock: red if a connector stops reading the field (the count then reads
``None``).
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.types import Usage as AnthropicUsage
from google.genai import types as genai_types
from openai.types import CompletionUsage

from course_supporter.llm.providers.dashscope import DashScopeProvider
from course_supporter.llm.providers.deepseek import DeepSeekProvider
from course_supporter.llm.schemas import LLMRequest


def _openai_compat_answering_with(usage: CompletionUsage) -> DeepSeekProvider:
    provider = DeepSeekProvider(
        api_keys=("k",),
        default_model="deepseek-flash",
        provider_name="deepseek",
        base_url="https://api.deepseek.com/v1",
    )
    choice = MagicMock()
    choice.message.content = "ok"
    choice.finish_reason = "stop"
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    provider._client_cycle = itertools.cycle([client])
    return provider


class TestDeepSeek:
    async def test_cache_hit_and_reasoning_tokens(self) -> None:
        # DeepSeek's usage shape: hit + miss = prompt_tokens, kept by the SDK
        # as extra fields; reasoning inside completion_tokens_details.
        usage = CompletionUsage.model_validate(
            {
                "prompt_tokens": 1200,
                "completion_tokens": 900,
                "total_tokens": 2100,
                "prompt_cache_hit_tokens": 1024,
                "prompt_cache_miss_tokens": 176,
                "completion_tokens_details": {"reasoning_tokens": 700},
            }
        )
        response = await _openai_compat_answering_with(usage).complete(
            LLMRequest(prompt="x")
        )
        assert response.tokens_in == 1200
        assert response.tokens_in_cached == 1024
        assert response.tokens_out == 900
        assert response.tokens_reasoning == 700

    async def test_openai_shaped_cached_tokens(self) -> None:
        usage = CompletionUsage.model_validate(
            {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "total_tokens": 510,
                "prompt_tokens_details": {"cached_tokens": 384},
            }
        )
        response = await _openai_compat_answering_with(usage).complete(
            LLMRequest(prompt="x")
        )
        assert response.tokens_in_cached == 384
        assert response.tokens_reasoning is None

    async def test_no_details_reported(self) -> None:
        usage = CompletionUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6)
        response = await _openai_compat_answering_with(usage).complete(
            LLMRequest(prompt="x")
        )
        assert response.tokens_in_cached is None
        assert response.tokens_reasoning is None


class TestGemini:
    async def test_cached_content_tokens(self) -> None:
        from course_supporter.llm.providers.gemini import GeminiProvider

        with patch("course_supporter.llm.providers.gemini.genai.Client"):
            provider = GeminiProvider(api_keys=("k",), default_model="gemini-3.8-flash")
        response = MagicMock()
        response.text = "ok"
        response.candidates = []
        response.usage_metadata = genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=3000,
            cached_content_token_count=2048,
            candidates_token_count=40,
        )
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        provider._client_cycle = itertools.cycle([client])

        result = await provider.complete(LLMRequest(prompt="x"))

        assert result.tokens_in == 3000
        assert result.tokens_in_cached == 2048


class TestDashScope:
    @pytest.mark.parametrize(
        ("prompt_details", "cached"),
        [({"cached_tokens": 1536}, 1536), ({}, None)],
        ids=["cache-hit", "empty-details-as-measured-live"],
    )
    async def test_cached_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prompt_details: dict[str, Any],
        cached: int | None,
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = DashScopeProvider(
            api_keys=("k",), default_model="qwen3.7-max", base_url=None
        )
        response = MagicMock()
        response.status_code = 200
        response.code = ""
        response.message = ""
        response.output = {"choices": [{"message": {"content": "ok"}}]}
        response.usage = {
            "input_tokens": 2000,
            "output_tokens": 30,
            "prompt_tokens_details": prompt_details,
        }
        monkeypatch.setattr(
            ds_module.AioGeneration, "call", AsyncMock(return_value=response)
        )

        result = await provider.complete(LLMRequest(prompt="hi", model="qwen3.7-max"))

        assert result.tokens_in == 2000
        assert result.tokens_in_cached == cached


class TestAnthropic:
    async def test_cache_reads_join_the_input_count(self) -> None:
        from course_supporter.llm.providers.anthropic import AnthropicProvider

        with patch("course_supporter.llm.providers.anthropic.anthropic.AsyncAnthropic"):
            provider = AnthropicProvider(api_keys=("k",), default_model="claude-x")
        response = MagicMock()
        response.content = [MagicMock(text="ok")]
        response.stop_reason = "end_turn"
        response.usage = AnthropicUsage(
            input_tokens=100, output_tokens=5, cache_read_input_tokens=900
        )
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=response)
        provider._client_cycle = itertools.cycle([client])

        result = await provider.complete(LLMRequest(prompt="x"))

        assert result.tokens_in == 1000
        assert result.tokens_in_cached == 900
