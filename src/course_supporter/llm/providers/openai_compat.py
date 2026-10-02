"""OpenAI-compatible provider (OpenAI + DeepSeek + Mistral).

Structured output goes through :meth:`OpenAICompatProvider.complete` like
any other call: the router-chosen schema mode is sent as ``response_format``
(task 09a).

Supports multimodal vision requests: when ``LLMRequest.contents``
contains ``bytes`` items they are sent as base64 inline images.
"""

from __future__ import annotations

import base64
import itertools
import re
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import openai
from openai.types.chat import ChatCompletionMessageParam

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.finish_reason import FinishReason, normalize_finish_reason
from course_supporter.llm.json_extract import strip_markdown_json
from course_supporter.llm.providers.base import LLMProvider, RequestConfigError
from course_supporter.llm.response_schema import mentions_json, openai_response_format
from course_supporter.llm.schemas import LLMRequest, LLMResponse, SchemaMode

# OpenAI populates BadRequestError.code from body["code"]. The
# canonical context-overflow code on OpenAI is "context_length_exceeded".
# DeepSeek / Mistral do not always set body["code"]; fall back to a
# message scan for those vendors.
_OPENAI_OVERFLOW_CODES = {"context_length_exceeded"}
_OPENAI_OVERFLOW_PATTERN = re.compile(
    r"context length|too long|maximum context|tokens exceed",
    re.IGNORECASE,
)

# Chat Completions ``choice.finish_reason`` vocabulary, shared by OpenAI,
# DeepSeek and Mistral. DeepSeek with thinking on reports "length" when the
# reasoning consumed the whole ``max_tokens`` and ``content`` came back empty.
_CEILING_FINISH_REASONS = frozenset({"length"})
_STOP_FINISH_REASONS = frozenset({"stop"})


def _normalize_choice_finish(raw: object) -> FinishReason:
    return normalize_finish_reason(
        raw, ceiling=_CEILING_FINISH_REASONS, stop=_STOP_FINISH_REASONS
    )


# Explicit SDK timeout. Read budget covers reasoning-tier providers
# (DeepSeek thinking-on observed 149-707s in TASK-2.4.17 live runs);
# connect kept short so DNS / TLS hiccups fail fast and the ladder
# escalates instead of hanging the ARQ event loop (TASK-2.4.18).
_DEFAULT_HTTP_TIMEOUT = httpx.Timeout(900.0, connect=30.0)


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


class OpenAICompatProvider(LLMProvider):
    """Provider for OpenAI API and compatible services (DeepSeek, Mistral).

    Uses the same OpenAI SDK with different ``base_url`` per provider.
    Structured output is the ``response_format`` that :meth:`complete`
    sends when the request carries a schema mode.

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
            openai.AsyncOpenAI(
                api_key=k, base_url=base_url, timeout=_DEFAULT_HTTP_TIMEOUT
            )
            for k in api_keys
        )
        self._client_cycle: Iterator[openai.AsyncOpenAI] = itertools.cycle(
            self._clients
        )

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

    def check_request(self, request: LLMRequest) -> None:
        """JSON mode needs the word "json" in the messages (task 09a).

        OpenAI and DeepSeek answer ``response_format={"type": "json_object"}``
        with HTTP 400 unless a message mentions JSON; Mistral shares this
        connector and the rule. Caught here, before the call, it is a
        configuration error the stage's prompt has to fix -- not a refusal
        that silently descends the ladder.
        """
        if request.schema_mode is SchemaMode.JSON and not mentions_json(request):
            msg = (
                f"{self.provider_name}: JSON mode requires the word 'json' in "
                f"the system or user message (stage '{request.action}')"
            )
            raise RequestConfigError(msg)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Classify OpenAI-SDK exceptions into ladder categories.

        Same classifier covers OpenAI, DeepSeek, and Mistral, all of
        which raise from the ``openai`` package via the shared client.

        Mapping:
        * RateLimitError / APITimeoutError / APIConnectionError /
          InternalServerError -> INFRASTRUCTURE.
        * ``httpx.TimeoutException`` / builtin ``TimeoutError``
          (alias of ``asyncio.TimeoutError`` since Py3.11) ->
          INFRASTRUCTURE. Covers transport-level timeouts the OpenAI
          SDK may surface unwrapped and outer ``asyncio.wait_for``
          guards layered around the call.
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
                httpx.TimeoutException,
                TimeoutError,
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

        # Task 09a: the schema mode chosen by the router goes on the wire as
        # its own kwarg, never merged into the vendor hook -- a request without
        # a schema mode sends exactly what it sent before.
        schema_kwargs: dict[str, Any] = {}
        response_format = openai_response_format(request)
        if response_format is not None:
            schema_kwargs["response_format"] = response_format

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                **schema_kwargs,
                **self._extra_create_kwargs(),
            )

        choice = response.choices[0]
        usage = response.usage
        content = choice.message.content or ""
        if request.expects_json:
            content = strip_markdown_json(content)
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            finish_reason=_normalize_choice_finish(choice.finish_reason),
            latency_ms=timer.elapsed_ms,
        )
