"""OpenAI-compatible provider (OpenAI + DeepSeek + Mistral).

Uses ``instructor`` for structured output: tool/function calling
with automatic retry on validation errors. This is more reliable
than embedding JSON schema in the system prompt.

Supports multimodal vision requests: when ``LLMRequest.contents``
contains ``bytes`` items they are sent as base64 inline images.
"""

from __future__ import annotations

import base64
import itertools
import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import openai
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.providers.base import LLMProvider, StructuredOutputError
from course_supporter.llm.schemas import LLMRequest, LLMResponse

# OpenAI populates BadRequestError.code from body["code"]. The
# canonical context-overflow code on OpenAI is "context_length_exceeded".
# DeepSeek / Mistral do not always set body["code"]; fall back to a
# message scan for those vendors.
_OPENAI_OVERFLOW_CODES = {"context_length_exceeded"}
_OPENAI_OVERFLOW_PATTERN = re.compile(
    r"context length|too long|maximum context|tokens exceed",
    re.IGNORECASE,
)


def _build_vision_content(
    images: list[bytes],
    prompt: str,
) -> list[dict[str, Any]]:
    """Build OpenAI vision content parts from raw image bytes + text.

    Each bytes item becomes an inline base64 image_url part.
    The prompt is appended as a text part.
    """
    parts: list[dict[str, Any]] = []
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            }
        )
    parts.append({"type": "text", "text": prompt})
    return parts


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

    def _extra_create_kwargs(self) -> dict[str, Any]:
        """Provider-specific extra kwargs spread into ``chat.completions.create``.

        Default returns ``{}``. Subclasses (e.g. ``DeepSeekProvider``)
        override to inject vendor-specific knobs such as
        ``extra_body={"thinking": {...}}`` without duplicating the
        base call body.
        """
        return {}

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Classify OpenAI-SDK exceptions into ladder categories.

        Same classifier covers OpenAI, DeepSeek, and Mistral, all of
        which raise from the ``openai`` package via the shared client.

        Mapping:
        * RateLimitError / APITimeoutError / APIConnectionError /
          InternalServerError -> INFRASTRUCTURE.
        * BadRequestError with code ``context_length_exceeded``, or a
          message matching a known overflow pattern (DeepSeek /
          Mistral fallback) -> INPUT_OVERFLOW.
        * Other BadRequestError -> SEMANTIC.
        * Anything else -> SEMANTIC (via base).
        """
        if isinstance(
            exc,
            (
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.InternalServerError,
            ),
        ):
            return ErrorCategory.INFRASTRUCTURE
        if isinstance(exc, openai.BadRequestError):
            if exc.code in _OPENAI_OVERFLOW_CODES:
                return ErrorCategory.INPUT_OVERFLOW
            if _OPENAI_OVERFLOW_PATTERN.search(exc.message or ""):
                return ErrorCategory.INPUT_OVERFLOW
            return ErrorCategory.SEMANTIC
        return super().classify_error(exc)

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
        """Generate text completion via OpenAI-compatible API.

        When ``request.contents`` contains bytes items, a multimodal
        vision message is built with inline base64 images.
        """
        model = request.model or self._default_model
        messages: list[ChatCompletionMessageParam] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})

        # Vision path: contents has image bytes
        image_items = [c for c in (request.contents or []) if isinstance(c, bytes)]
        if image_items:
            parts = _build_vision_content(image_items, request.prompt)
            messages.append({"role": "user", "content": parts})  # type: ignore[arg-type,misc]
        else:
            messages.append({"role": "user", "content": request.prompt})

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                **self._extra_create_kwargs(),
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
                    **self._extra_create_kwargs(),
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
