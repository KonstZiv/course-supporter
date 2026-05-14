"""Unit tests for :class:`DeepSeekProvider` thinking-mode override.

Verifies the KD-2.1-O contract that DeepSeek production calls always
spread ``extra_body={"thinking": {"type": "disabled"}}`` into the
underlying OpenAI-compatible client. Without this override the
default thinking-on behaviour inflates output ~6x and latency ~7x
(measured during Phase A spike, 2026-05-12).

The tests use light fakes for the OpenAI client cycle and the
instructor cycle so we can capture the kwargs passed into
``chat.completions.create`` / ``create_with_completion`` without
hitting the real DeepSeek API.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from course_supporter.llm.providers.deepseek import DeepSeekProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest

if TYPE_CHECKING:
    from collections.abc import Iterator


def _make_provider() -> DeepSeekProvider:
    """Construct a DeepSeekProvider with a single fake API key."""
    return DeepSeekProvider(
        api_keys=("test-key",),
        default_model="deepseek-v4-flash",
        provider_name="deepseek",
        base_url="https://api.deepseek.com/v1",
    )


def _stub_completion_response() -> MagicMock:
    """Minimal mock matching the attribute access pattern in ``complete``."""
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = '{"ok": true}'
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=42, completion_tokens=7)
    return response


def _stub_structured_completion() -> MagicMock:
    """Mock returned alongside the parsed instance from instructor."""
    completion = MagicMock()
    choice = MagicMock()
    choice.message.content = '{"ok": true}'
    completion.choices = [choice]
    completion.usage = MagicMock(prompt_tokens=42, completion_tokens=7)
    return completion


class _SchemaForTest(BaseModel):
    """Minimal schema used as ``response_model`` placeholder."""

    ok: bool


class TestDeepSeekProviderExtraBody:
    """KD-2.1-O: thinking-disabled forced on every DeepSeek call."""

    def test_inherits_from_openai_compat(self) -> None:
        provider = _make_provider()
        assert isinstance(provider, OpenAICompatProvider)

    def test_extra_create_kwargs_returns_thinking_disabled(self) -> None:
        """Hook returns exactly the body fragment the SDK expects."""
        provider = _make_provider()
        kwargs = provider._extra_create_kwargs()
        assert kwargs == {"extra_body": {"thinking": {"type": "disabled"}}}

    def test_base_class_extra_kwargs_remains_empty(self) -> None:
        """Sanity check: parent (openai / mistral) path is unaffected."""
        base = OpenAICompatProvider(
            api_keys=("test-key",),
            default_model="gpt-test",
            provider_name="openai",
        )
        assert base._extra_create_kwargs() == {}

    @pytest.mark.asyncio
    async def test_complete_passes_thinking_disabled_in_extra_body(self) -> None:
        """``complete`` spreads ``extra_body`` into ``chat.completions.create``."""
        provider = _make_provider()
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(
            return_value=_stub_completion_response()
        )

        # Force the provider to use our fake client by replacing the cycle.
        cycle: Iterator[Any] = itertools.cycle([fake_client])
        provider._client_cycle = cycle  # type: ignore[assignment]

        request = LLMRequest(
            prompt="hello",
            system_prompt="sys",
            model="deepseek-v4-flash",
            temperature=0.0,
            max_tokens=1024,
        )

        await provider.complete(request)

        fake_client.chat.completions.create.assert_awaited_once()
        kwargs = fake_client.chat.completions.create.await_args.kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        # Sanity: base kwargs still pass through unchanged.
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["temperature"] == 0.0
        assert kwargs["max_tokens"] == 1024
        assert kwargs["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_complete_structured_passes_thinking_disabled(self) -> None:
        """instructor's ``create_with_completion`` also receives ``extra_body``."""
        provider = _make_provider()

        fake_instructor = MagicMock()
        parsed_instance = _SchemaForTest(ok=True)
        fake_instructor.chat.completions.create_with_completion = AsyncMock(
            return_value=(parsed_instance, _stub_structured_completion())
        )
        # Skip the lazy instructor bootstrap; install our fake cycle directly.
        provider._instructor_clients = (fake_instructor,)  # type: ignore[assignment]
        provider._instructor_cycle = itertools.cycle((fake_instructor,))

        request = LLMRequest(
            prompt="hello",
            system_prompt=None,
            model="deepseek-v4-flash",
            temperature=0.0,
            max_tokens=256,
        )

        await provider.complete_structured(request, _SchemaForTest)

        instructor_create = fake_instructor.chat.completions.create_with_completion
        instructor_create.assert_awaited_once()
        kwargs = instructor_create.await_args.kwargs
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["max_retries"] == 2
        assert kwargs["response_model"] is _SchemaForTest

    @pytest.mark.asyncio
    async def test_openai_compat_complete_omits_extra_body(self) -> None:
        """Non-DeepSeek providers must NOT inject ``extra_body``."""
        base = OpenAICompatProvider(
            api_keys=("test-key",),
            default_model="gpt-test",
            provider_name="openai",
        )
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(
            return_value=_stub_completion_response()
        )
        base._client_cycle = itertools.cycle([fake_client])  # type: ignore[assignment]

        request = LLMRequest(
            prompt="hi",
            model="gpt-test",
            temperature=0.0,
        )

        await base.complete(request)

        fake_client.chat.completions.create.assert_awaited_once()
        kwargs = fake_client.chat.completions.create.await_args.kwargs
        # Base class returns ``{}`` from the hook, so ``extra_body`` must
        # not appear at all in the call kwargs.
        assert "extra_body" not in kwargs


class TestProviderRegistryDeepSeek:
    """Registry now maps ``deepseek`` to the specialised subclass."""

    def test_registry_uses_deepseek_subclass(self) -> None:
        from course_supporter.llm.providers import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["deepseek"] is DeepSeekProvider

    def test_factory_creates_deepseek_provider_instance(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers

        s = Settings(
            deepseek_api_key="test-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        providers = create_providers(s)
        assert isinstance(providers["deepseek"], DeepSeekProvider)
        # Subclass relationship preserved for any downstream isinstance
        # checks against the parent class.
        assert isinstance(providers["deepseek"], OpenAICompatProvider)
