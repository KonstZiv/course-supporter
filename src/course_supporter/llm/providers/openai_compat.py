"""OpenAI-compatible provider (OpenAI + DeepSeek + Mistral).

Uses ``instructor`` for structured output: tool/function calling
with automatic retry on validation errors. This is more reliable
than embedding JSON schema in the system prompt.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import openai
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.schemas import LLMRequest, LLMResponse

if TYPE_CHECKING:
    import instructor


class OpenAICompatProvider(LLMProvider):
    """Provider for OpenAI API and compatible services (DeepSeek, Mistral).

    Uses the same OpenAI SDK with different ``base_url`` per provider.
    Structured output uses ``instructor`` for tool-based schema
    enforcement with automatic validation retry.

    When multiple API keys are provided, SDK clients are
    pre-created and rotated in round-robin order per request.
    """

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str,
        provider_name: str = "openai",
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.provider_name = provider_name
        self._default_model = default_model
        self._api_keys = tuple(api_keys)
        self._base_url = base_url
        self._clients = tuple(
            openai.AsyncOpenAI(api_key=k, base_url=base_url) for k in api_keys
        )
        self._client_cycle: Iterator[openai.AsyncOpenAI] = itertools.cycle(
            self._clients
        )
        # Instructor clients and exception class created lazily
        # on first complete_structured() call to avoid slow startup.
        self._instructor_clients: tuple[instructor.AsyncInstructor, ...] | None = None
        self._instructor_cycle: Iterator[instructor.AsyncInstructor] | None = None
        self._retry_exc_cls: type[Exception] = Exception

    def _next_client(self) -> openai.AsyncOpenAI:
        return next(self._client_cycle)

    def _ensure_instructor(self) -> None:
        """Lazily create instructor-patched clients on first use."""
        if self._instructor_clients is not None:
            return
        import instructor as _instructor
        from instructor.exceptions import (
            InstructorRetryException,
        )

        self._instructor_clients = tuple(
            _instructor.from_openai(
                openai.AsyncOpenAI(api_key=k, base_url=self._base_url)
            )
            for k in self._api_keys
        )
        self._instructor_cycle = itertools.cycle(self._instructor_clients)
        self._retry_exc_cls = InstructorRetryException

    def _next_instructor_client(self) -> instructor.AsyncInstructor:
        self._ensure_instructor()
        # _ensure_instructor guarantees these are set
        return next(self._instructor_cycle)  # type: ignore[arg-type]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via OpenAI-compatible API."""
        model = request.model or self._default_model
        messages: list[ChatCompletionMessageParam] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )

        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            content=choice.message.content or "",
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output via instructor (tool/function calling).

        Instructor handles schema enforcement via tool definitions and
        automatic retry with Pydantic validation error feedback.
        Falls back to StructuredOutputError on persistent failure.
        """
        model = request.model or self._default_model
        messages: list[ChatCompletionMessageParam] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        client = self._next_instructor_client()
        with self._measure_latency() as timer:
            try:
                (
                    result,
                    completion,
                ) = await client.chat.completions.create_with_completion(
                    model=model,
                    messages=messages,
                    response_model=response_schema,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    max_retries=2,
                )
            except Exception as exc:
                if not isinstance(exc, self._retry_exc_cls):
                    raise
                raise StructuredOutputError(
                    provider=self.provider_name,
                    raw_content=str(exc),
                    schema_name=response_schema.__name__,
                    cause=exc,
                ) from exc

        usage = completion.usage
        llm_response = LLMResponse(
            content=completion.choices[0].message.content or ""
            if completion.choices
            else "",
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=timer.elapsed_ms,
        )
        return result, llm_response
