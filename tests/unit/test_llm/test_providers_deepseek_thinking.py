"""Unit tests for :class:`DeepSeekThinkingProvider` (KD-2.4-T).

Mirror of :mod:`tests.unit.test_llm.test_providers_deepseek` for the
thinking-on sibling. Verifies that:

* the subclass inherits :class:`OpenAICompatProvider` cleanly,
* :meth:`_extra_create_kwargs` returns the empty base-class default
  (NOT the thinking-disabled override from KD-2.4-S),
* both call paths (``chat.completions.create`` and instructor's
  ``create_with_completion``) reach the SDK with no ``extra_body``
  injected, so the DeepSeek API applies its default thinking-on V4
  behaviour,
* the provider registry exposes the new class under
  ``"deepseek_thinking"`` and the factory wires it from the shared
  ``DEEPSEEK_API_KEY`` pool, returning a working instance alongside the
  non-thinking :class:`DeepSeekProvider`.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from course_supporter.llm.providers.deepseek import DeepSeekProvider
from course_supporter.llm.providers.deepseek_thinking import DeepSeekThinkingProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest

if TYPE_CHECKING:
    from collections.abc import Iterator


def _make_provider() -> DeepSeekThinkingProvider:
    """Construct a DeepSeekThinkingProvider with a single fake API key."""
    return DeepSeekThinkingProvider(
        api_keys=("test-key",),
        default_model="deepseek-v4-pro",
        provider_name="deepseek_thinking",
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


class TestDeepSeekThinkingProviderHook:
    """KD-2.4-T: thinking-on default kept by omitting the disable override."""

    def test_inherits_from_openai_compat(self) -> None:
        provider = _make_provider()
        assert isinstance(provider, OpenAICompatProvider)

    def test_extra_create_kwargs_returns_empty(self) -> None:
        """The base-class neutral hook is exactly what we want here."""
        provider = _make_provider()
        assert provider._extra_create_kwargs() == {}

    def test_distinct_from_non_thinking_provider(self) -> None:
        """The two DeepSeek classes are NOT the same — non-think keeps disable."""
        thinking = _make_provider()
        non_thinking = DeepSeekProvider(
            api_keys=("test-key",),
            default_model="deepseek-v4-flash",
            provider_name="deepseek",
            base_url="https://api.deepseek.com/v1",
        )
        assert type(thinking) is not type(non_thinking)
        assert thinking._extra_create_kwargs() == {}
        assert non_thinking._extra_create_kwargs() == {
            "extra_body": {"thinking": {"type": "disabled"}}
        }

    @pytest.mark.asyncio
    async def test_complete_omits_extra_body(self) -> None:
        """``complete`` does NOT inject ``extra_body`` — thinking stays default-on."""
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
            model="deepseek-v4-pro",
            temperature=0.0,
            max_tokens=1024,
        )

        await provider.complete(request)

        fake_client.chat.completions.create.assert_awaited_once()
        kwargs = fake_client.chat.completions.create.await_args.kwargs
        # KD-2.4-T contract: NO extra_body, so DeepSeek defaults to thinking-on.
        assert "extra_body" not in kwargs
        # Sanity: base kwargs still pass through unchanged.
        assert kwargs["model"] == "deepseek-v4-pro"
        assert kwargs["temperature"] == 0.0
        assert kwargs["max_tokens"] == 1024
        assert kwargs["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_complete_structured_omits_extra_body(self) -> None:
        """instructor's ``create_with_completion`` also runs without ``extra_body``."""
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
            model="deepseek-v4-pro",
            temperature=0.0,
            max_tokens=256,
        )

        await provider.complete_structured(request, _SchemaForTest)

        instructor_create = fake_instructor.chat.completions.create_with_completion
        instructor_create.assert_awaited_once()
        kwargs = instructor_create.await_args.kwargs
        assert "extra_body" not in kwargs
        assert kwargs["model"] == "deepseek-v4-pro"
        assert kwargs["max_retries"] == 2
        assert kwargs["response_model"] is _SchemaForTest


class TestProviderRegistryDeepSeekThinking:
    """Registry exposes the new class alongside the non-thinking default."""

    def test_registry_maps_deepseek_thinking_to_subclass(self) -> None:
        from course_supporter.llm.providers import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["deepseek_thinking"] is DeepSeekThinkingProvider
        # Non-think mapping must stay untouched (KD-2.4-S contract preserved).
        assert PROVIDER_REGISTRY["deepseek"] is DeepSeekProvider

    def test_factory_shares_deepseek_key_pool(self) -> None:
        """``DEEPSEEK_API_KEY`` ENV powers both providers via aliased Settings field."""
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers

        s = Settings(
            deepseek_api_key="test-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        providers = create_providers(s)
        assert isinstance(providers["deepseek"], DeepSeekProvider)
        assert isinstance(providers["deepseek_thinking"], DeepSeekThinkingProvider)
        # Subclass relationship preserved for any downstream isinstance checks.
        assert isinstance(providers["deepseek_thinking"], OpenAICompatProvider)
