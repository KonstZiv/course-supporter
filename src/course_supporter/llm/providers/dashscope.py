"""Alibaba DashScope provider for Qwen multimodal AND text models.

Supports standard regions (Singapore via dashscope-intl, US, Beijing)
and MaaS workspace endpoints (eu-central-1, etc.) via module-level
``dashscope.base_http_api_url`` configured at provider init time.

KD-2.3-A: native client per provider — does NOT reuse
``openai_compat.py`` compatible-mode shim. Reasons:

1. Silent reasoning-mode override drop risk. Compatible-mode strips
   provider-specific knobs that affect output structure for VL models.
2. Multimodal encoding nuances. DashScope native expects
   ``content: [{"image": "data:image/...;base64,..."}, {"text": "..."}]``,
   not OpenAI's ``image_url`` wrapper.
3. Cost calculation hook gap. DashScope returns ``usage.input_tokens``
   / ``usage.output_tokens`` per its own response shape (also
   ``usage.image`` / ``usage.video`` fields for VL); compatible mode
   masks these.

DD-2.4-O: ``complete`` routes between two distinct SDK endpoints by
the presence of image bytes in ``request.contents``:

* image bytes present → ``AioMultiModalConversation.call``
  (``multimodal-generation`` task-group; VL models like
  ``qwen3-vl-32b-instruct``).
* no image bytes → ``AioGeneration.call`` (``text-generation``
  task-group; text reasoning flagships like
  ``qwen3.7-max-2026-05-20``).

The split is required because the SDK 1.25.18 cannot serve a text
model through the multimodal endpoint — the server returns a non-JSON
4xx and the SDK's aiohttp branch fails to decode it
(``StreamReader`` vs ``bytes`` mismatch in
``dashscope/common/utils.py``), surfacing as
``'StreamReader' object has no attribute 'decode'`` ~289 ms before
inference.

DashScope surfaces most errors as non-200 ``DashScopeAPIResponse``
objects rather than exceptions. ``DashScopeResponseError`` wraps the
status_code + code + message tuple so ``classify_error`` can map to
``ErrorCategory`` consistently with other providers; the shape is
identical across both endpoints.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
from collections.abc import Iterator, Sequence
from typing import Any

import dashscope
from dashscope.aigc.generation import AioGeneration
from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from dashscope.common.error import (
    AuthenticationError,
    DashScopeException,
    InvalidInput,
    InvalidParameter,
    UnsupportedModel,
)
from pydantic import BaseModel

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.json_extract import strip_markdown_json
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse

# HTTP status codes returned in DashScopeAPIResponse.status_code.
_HTTP_RATE_LIMITED = frozenset({429})
_HTTP_TIMEOUT = frozenset({408, 504})
_HTTP_SERVER_ERROR = frozenset({500, 502, 503})
_HTTP_PAYLOAD_TOO_LARGE = frozenset({413})

# DashScope error code prefixes that indicate context-overflow.
_OVERFLOW_CODE_PREFIXES = (
    "InvalidParameter.MaxTokenExceed",
    "InvalidParameter.InputTooLong",
    "InvalidParameter.ContextTooLong",
)
# Each branch requires both a domain term (input/context/tokens) AND an
# overflow verb (too long / exceed) to avoid false-positive matches on
# unrelated 400 messages that happen to mention "max tokens" or
# "context length". Aligns with providers/gemini.py strictness.
_OVERFLOW_MESSAGE_PATTERN = re.compile(
    r"input (?:length|token) (?:too long|exceed)|"
    r"context (?:length|window) exceed|"
    r"max(?:imum)? (?:input |context )?tokens? exceed",
    re.IGNORECASE,
)

# Image MIME signatures for inline base64 data URI scheme.
_IMAGE_MIME_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes.

    Mirrors the convention used by ``providers/gemini.py``: falls back to
    ``application/octet-stream`` for unknown formats so the SDK surfaces
    a clear error rather than silently mislabeling bytes. DashScope
    accepts JPEG/PNG/GIF/WEBP; any other MIME (including the fallback)
    triggers the SDK's ``InvalidParameter`` path at call time. We
    deliberately defer that rejection to the SDK boundary instead of
    raising an in-process error here, so the failure surfaces alongside
    the rest of the DashScope error taxonomy that ``classify_error``
    already maps to ``ErrorCategory``.
    """
    for signature, mime in _IMAGE_MIME_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class DashScopeResponseError(Exception):
    """Raised when DashScope returns a non-200 response.

    DashScope SDK does not raise exceptions for HTTP errors;
    callers must inspect ``response.status_code`` and surface
    failures themselves. This wrapper carries the three diagnostic
    fields ``classify_error`` needs to assign an ``ErrorCategory``.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"DashScope HTTP {status_code} {code}: {message}")


class DashScopeProvider(LLMProvider):
    """Alibaba DashScope provider for Qwen VL and text-reasoning models.

    The DashScope Python SDK uses module-level globals for
    ``api_key`` and ``base_http_api_url``. We set ``base_http_api_url``
    once at init time (binds all DashScope calls to one endpoint —
    standard region or MaaS workspace; applies uniformly to the
    multimodal and text-generation task-groups), and pass
    ``api_key=`` per call to support multi-key round-robin rotation
    across quotas.
    """

    provider_name = "dashscope"

    # Qwen3-VL-32B-instruct supports up to 8192 output tokens; we cap at
    # 4096 by default as a conservative cost guard for unbounded calls.
    # Production ladders override per-action via ``LLMRequest.max_tokens``
    # whenever a longer response is genuinely required.
    DEFAULT_MAX_TOKENS = 4096
    default_max_output_tokens: int = DEFAULT_MAX_TOKENS

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self._api_keys = tuple(api_keys)
        self._key_cycle: Iterator[str] = itertools.cycle(self._api_keys)
        self._default_model = default_model
        if base_url:
            # SDK does not support per-client base URLs; the global
            # write is intentional and binds the whole process to
            # this endpoint (workspace-scoped MaaS or regional standard).
            dashscope.base_http_api_url = base_url

    def _next_key(self) -> str:
        return next(self._key_cycle)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Map DashScope-surfaced failures to ladder categories.

        Mapping:
        * ``DashScopeResponseError`` with status 429/408/504/5xx
          -> INFRASTRUCTURE.
        * Status 413 / 400 matching overflow code prefix or pattern
          -> INPUT_OVERFLOW.
        * Status 400 / 401 / 403 / other 4xx -> SEMANTIC.
        * ``AuthenticationError`` / ``UnsupportedModel`` -> SEMANTIC.
        * ``InvalidInput`` / ``InvalidParameter`` -> SEMANTIC.
        * Other ``DashScopeException`` subclasses -> SEMANTIC via base.
        """
        if isinstance(exc, DashScopeResponseError):
            if exc.status_code in _HTTP_RATE_LIMITED:
                return ErrorCategory.INFRASTRUCTURE
            if exc.status_code in _HTTP_TIMEOUT:
                return ErrorCategory.INFRASTRUCTURE
            if exc.status_code in _HTTP_SERVER_ERROR:
                return ErrorCategory.INFRASTRUCTURE
            if exc.status_code in _HTTP_PAYLOAD_TOO_LARGE:
                return ErrorCategory.INPUT_OVERFLOW
            if exc.status_code == 400:
                if any(exc.code.startswith(p) for p in _OVERFLOW_CODE_PREFIXES):
                    return ErrorCategory.INPUT_OVERFLOW
                if _OVERFLOW_MESSAGE_PATTERN.search(exc.message):
                    return ErrorCategory.INPUT_OVERFLOW
                return ErrorCategory.SEMANTIC
            return ErrorCategory.SEMANTIC
        if isinstance(
            exc,
            (
                AuthenticationError,
                UnsupportedModel,
                InvalidInput,
                InvalidParameter,
            ),
        ):
            return ErrorCategory.SEMANTIC
        if isinstance(exc, DashScopeException):
            return ErrorCategory.SEMANTIC
        return super().classify_error(exc)

    def _build_messages(self, request: LLMRequest) -> list[dict[str, Any]]:
        """Build DashScope-shape messages from LLMRequest.

        Vision contents (raw bytes items in ``request.contents``) are
        encoded as ``data:image/<mime>;base64,<data>`` and emitted as
        ``{"image": "..."}`` content elements. Text follows as
        ``{"text": "..."}``.
        """
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": [{"text": request.system_prompt}],
                }
            )

        content: list[dict[str, Any]] = []
        for item in request.contents or []:
            if isinstance(item, (bytes, bytearray)):
                data = bytes(item)
                mime = _detect_image_mime(data)
                b64 = base64.b64encode(data).decode("ascii")
                content.append({"image": f"data:{mime};base64,{b64}"})
        if request.prompt:
            content.append({"text": request.prompt})
        messages.append({"role": "user", "content": content})
        return messages

    def _extract_text(self, response: Any) -> str:
        """Extract assistant text from DashScope response output.

        Response shape: ``output.choices[0].message.content`` is a
        ``list[dict]`` like ``[{"text": "..."}]``. Falls back to
        ``output.text`` for simpler single-turn responses, and to
        plain string content for compatibility.

        DashScope SDK output objects extend ``DictMixin`` and always
        expose ``.get()``, so we call it directly without defensive
        ``hasattr`` guards.
        """
        output = response.output
        if not output:
            return ""
        text_field = output.get("text")
        if text_field:
            return str(text_field)
        choices = output.get("choices")
        if not choices:
            return ""
        message = choices[0].get("message")
        if not message:
            return ""
        content = message.get("content")
        if not content:
            return ""
        if isinstance(content, str):
            return content
        texts: list[str] = []
        for elem in content:
            if isinstance(elem, dict) and "text" in elem and elem["text"]:
                texts.append(elem["text"])
        return "".join(texts)

    def _build_messages_text(self, request: LLMRequest) -> list[dict[str, Any]]:
        """Build DashScope text-generation messages from LLMRequest.

        The ``text-generation`` task-group expects ``content`` as a
        plain string per message (per
        ``dashscope.aigc.generation.Generation._build_input_parameters``,
        which assembles ``msgs.append({"role": Role.USER, "content": prompt})``).
        This shape differs from the multimodal task-group, which
        carries a ``list[dict]`` of ``{"image": ...}`` / ``{"text": ...}``
        elements. ``request.contents`` is intentionally ignored on this
        branch — callers must route bytes content through
        ``_build_messages`` instead.
        """
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        if request.prompt:
            messages.append({"role": "user", "content": request.prompt})
        return messages

    def _extract_text_generation(self, response: Any) -> str:
        """Extract assistant text from a DashScope ``Generation`` response.

        The text-generation endpoint returns one of two shapes per the
        SDK's ``result_format`` parameter
        (``dashscope/aigc/generation.py`` docstring, line 362):

        * default (``result_format="text"``) — ``output.text`` as a
          plain string.
        * ``result_format="message"`` —
          ``output.choices[0].message.content`` as a plain string per
          ``Message.from_generation_response``
          (``dashscope/api_entities/dashscope_response.py:132``).

        Both forms are handled here so future ladder rungs may pin
        either without coupling the extractor to a single shape.
        """
        output = response.output
        if not output:
            return ""
        text_field = output.get("text")
        if text_field:
            return str(text_field)
        choices = output.get("choices")
        if not choices:
            return ""
        message = choices[0].get("message")
        if not message:
            return ""
        content = message.get("content")
        if not content:
            return ""
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _has_image_bytes(request: LLMRequest) -> bool:
        """Return True iff ``request.contents`` carries image bytes.

        Routes ``complete`` between the multimodal and text-generation
        SDK endpoints (DD-2.4-O). ``None`` and empty-list contents are
        text-branch (no images); a contents list with at least one
        ``bytes``/``bytearray`` element is VL-branch. Non-bytes elements
        (e.g. plain text/URL items) do not by themselves switch the
        branch — only raw image bytes do.
        """
        contents = request.contents
        if not contents:
            return False
        return any(isinstance(item, (bytes, bytearray)) for item in contents)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate completion via the appropriate DashScope endpoint.

        Routes between ``AioMultiModalConversation.call`` (VL models,
        image bytes in ``request.contents``) and ``AioGeneration.call``
        (text-only reasoning models). The two endpoints share
        identical multi-key / base-URL plumbing, error taxonomy
        (``DashScopeResponseError``), and usage shape
        (``input_tokens`` / ``output_tokens``); only the message shape
        and the extractor differ per task-group. DD-2.4-O.
        """
        model = request.model or self._default_model
        has_images = self._has_image_bytes(request)

        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": self._next_key(),
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.reasoning is not None:
            # Per-rung reasoning override (e.g. ``{"exclude": True}``)
            # propagated verbatim on both branches per KD-2.3-S — the
            # SDK forwards the kwarg to the Qwen reasoning-mode control
            # symmetrically for VL and text models; omitting it
            # preserves each model's default behaviour. No live ladder
            # rung currently sets this on the text branch; the
            # passthrough is mechanism-only.
            kwargs["reasoning"] = request.reasoning

        with self._measure_latency() as timer:
            if has_images:
                kwargs["messages"] = self._build_messages(request)
                response = await AioMultiModalConversation.call(**kwargs)
            else:
                kwargs["messages"] = self._build_messages_text(request)
                response = await AioGeneration.call(**kwargs)

        if response.status_code != 200:
            raise DashScopeResponseError(
                status_code=response.status_code,
                code=response.code or "",
                message=response.message or "",
            )

        if has_images:
            content_text = self._extract_text(response)
        else:
            content_text = self._extract_text_generation(response)
        if request.expects_json:
            content_text = strip_markdown_json(content_text)
        usage = response.usage
        tokens_in = usage.get("input_tokens") if usage else None
        tokens_out = usage.get("output_tokens") if usage else None

        return LLMResponse(
            content=content_text,
            provider=self.provider_name,
            model_id=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output via system-prompt JSON injection.

        Qwen3-VL does not expose OpenAI-style tool calling through
        DashScope MultiModalConversation, so we mirror the pattern
        used by AnthropicProvider: inject the JSON schema into the
        system prompt and parse the response, stripping markdown
        fences if the model emits them.
        """
        schema_json = json.dumps(
            response_schema.model_json_schema(), ensure_ascii=False
        )
        structured_system = (
            f"{request.system_prompt or ''}\n\n"
            f"Respond ONLY with raw JSON matching this schema, "
            f"no markdown fences:\n{schema_json}"
        )
        modified_request = request.model_copy(
            update={"system_prompt": structured_system}
        )
        llm_response = await self.complete(modified_request)
        raw = strip_markdown_json(llm_response.content)
        parsed = self._parse_structured(raw, response_schema)
        return parsed, llm_response
