"""OpenAI-compatible provider (OpenAI + DeepSeek + Mistral).

Uses ``instructor`` for structured output: tool/function calling
with automatic retry on validation errors. This is more reliable
than embedding JSON schema in the system prompt.
"""

import itertools
from collections.abc import Iterator, Sequence
from typing import Any

import instructor
import openai
from instructor.exceptions import InstructorRetryException
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.schemas import LLMRequest, LLMResponse


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
        self._clients = tuple(
            openai.AsyncOpenAI(api_key=k, base_url=base_url) for k in api_keys
        )
        self._client_cycle: Iterator[openai.AsyncOpenAI] = itertools.cycle(
            self._clients
        )
        self._instructor_clients = tuple(
            instructor.from_openai(openai.AsyncOpenAI(api_key=k, base_url=base_url))
            for k in api_keys
        )
        self._instructor_cycle: Iterator[instructor.AsyncInstructor] = itertools.cycle(
            self._instructor_clients
        )

    def _next_client(self) -> openai.AsyncOpenAI:
        return next(self._client_cycle)

    def _next_instructor_client(self) -> instructor.AsyncInstructor:
        return next(self._instructor_cycle)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via OpenAI-compatible API."""
        model = request.model or self._default_model
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
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
            except InstructorRetryException as exc:
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
