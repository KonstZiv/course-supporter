"""Locks: no provider SDK client retries on its own (BIL-D).

Every retry belongs to our layers — the ladder (``llm/stage_router.py``, KD16)
and the speech-to-text router (``stt/router.py``) — which write each attempt to
the call register. A retry inside an SDK is a second, unseen bill for one
register row. These tests fail if a client starts retrying again: either a
constructor loses its setting, or an SDK changes its default.

The OpenAI-compatible and Anthropic clients are checked by behaviour: a 503
answer is sent back once and the request count must stay at one (with the SDK
default of two retries it would be three).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import aiohttp
import anthropic
import httpx
import openai
import pytest
from google.genai._api_client import retry_args

from course_supporter.llm.providers.anthropic import AnthropicProvider
from course_supporter.llm.providers.dashscope import DashScopeProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
from course_supporter.stt.providers.elevenlabs import ElevenLabsSTTProvider
from course_supporter.stt.providers.openai_stt import OpenAISTTProvider


async def _requests_sent(call: Any, error: type[BaseException]) -> int:
    """How many HTTP requests ``call`` sends when every answer is a 503."""
    calls = 0

    async def send(
        _client: httpx.AsyncClient, request: httpx.Request, **_: Any
    ) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            request=request,
            json={"error": {"message": "overloaded", "type": "server_error"}},
        )

    with patch.object(httpx.AsyncClient, "send", send), pytest.raises(error):
        await call()
    return calls


class TestOpenAICompatible:
    def test_every_client_is_built_without_retries(self) -> None:
        provider = OpenAICompatProvider(["k1", "k2"], default_model="m")
        assert [c.max_retries for c in provider._clients] == [0, 0]

    async def test_a_503_is_sent_once(self) -> None:
        provider = OpenAICompatProvider(["k"], default_model="m")
        client = provider._clients[0]

        async def call() -> object:
            return await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )

        assert await _requests_sent(call, openai.APIStatusError) == 1


class TestAnthropic:
    def test_every_client_is_built_without_retries(self) -> None:
        provider = AnthropicProvider(["k1", "k2"], default_model="m")
        assert [c.max_retries for c in provider._clients] == [0, 0]

    async def test_a_503_is_sent_once(self) -> None:
        client = AnthropicProvider(["k"], default_model="m")._clients[0]

        async def call() -> object:
            return await client.messages.create(
                model="m",
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )

        assert await _requests_sent(call, anthropic.APIStatusError) == 1


class TestGemini:
    def test_every_client_makes_one_attempt(self) -> None:
        provider = GeminiProvider(["k1", "k2"], default_model="m")
        for client in provider._clients:
            options = client._api_client._http_options.retry_options
            # ``attempts`` counts the original request: 1 = no retry. The SDK
            # reads 0 as "not set" (5 attempts), so 1 is the only safe value.
            assert options is not None
            assert options.attempts == 1
            stop = retry_args(options)["stop"]
            assert stop.max_attempt_number == 1


class TestDashScope:
    async def test_the_async_path_sends_once(self) -> None:
        """The SDK's async path has no retry setting and no retry: one request.

        The provider calls ``AioGeneration`` / ``AioMultiModalConversation``,
        which post once through ``aiohttp``. The SDK's only retry setting
        (``requests`` adapter ``max_retries``) belongs to its synchronous
        connection pool, which the provider never uses.
        """
        from dashscope.aigc.generation import AioGeneration

        DashScopeProvider(["k"], default_model="qwen3.7-max")
        calls = 0

        async def failing_request(*_: Any, **__: Any) -> None:
            nonlocal calls
            calls += 1
            raise aiohttp.ServerDisconnectedError()

        with (
            patch.object(aiohttp.ClientSession, "_request", failing_request),
            pytest.raises(aiohttp.ServerDisconnectedError),
        ):
            await AioGeneration.call(model="qwen3.7-max", prompt="hi", api_key="k")
        assert calls == 1


class TestSpeechToText:
    def test_openai_stt_clients_are_built_without_retries(self) -> None:
        provider = OpenAISTTProvider(["k1", "k2"])
        assert [c.max_retries for c in provider._clients] == [0, 0]

    async def test_openai_stt_a_503_is_sent_once(self) -> None:
        client = OpenAISTTProvider(["k"])._clients[0]

        async def call() -> object:
            return await client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe", file=("a.wav", b"RIFF")
            )

        assert await _requests_sent(call, openai.APIStatusError) == 1

    @pytest.mark.parametrize(
        "provider_cls", [ElevenLabsSTTProvider, DeepgramSTTProvider]
    )
    def test_raw_httpx_clients_have_no_transport_retries(
        self, provider_cls: type[ElevenLabsSTTProvider | DeepgramSTTProvider]
    ) -> None:
        """Raw ``httpx``: no SDK; the transport's connect retries stay at 0."""
        provider = provider_cls(["k1", "k2"])
        for client in provider._clients:
            transport = client._transport
            assert isinstance(transport, httpx.AsyncHTTPTransport)
            assert transport._pool._retries == 0
