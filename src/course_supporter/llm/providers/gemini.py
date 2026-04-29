"""Google Gemini provider via google-genai SDK."""

import itertools
import re
from collections.abc import Iterator, Sequence
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse

# Gemini surfaces context overflow as ClientError code=400 with a
# message describing the limit; HTTP 413 is also possible. Pattern
# matches both Google API phrasing variants.
_GEMINI_OVERFLOW_PATTERN = re.compile(
    r"too long|token (?:limit|count) exceed|context (?:length|window) exceed",
    re.IGNORECASE,
)

# Image MIME signatures (magic bytes). Only the formats Gemini accepts
# and that we actually produce are listed: VD frame_sampler emits JPEG
# (ffmpeg frame_%06d.jpg), describe_slides emits PNG (PyMuPDF
# pixmap.tobytes("png")). WebP/HEIC/HEIF are left in because Gemini
# supports them and users may one day paste such content.
_IMAGE_MIME_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes.

    Falls back to ``application/octet-stream`` for unknown formats so the
    SDK surfaces a clear error rather than silently mislabeling bytes.
    """
    for signature, mime in _IMAGE_MIME_SIGNATURES:
        if data.startswith(signature):
            return mime
    # RIFF....WEBP needs a two-segment check.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _build_contents(request: LLMRequest) -> str | list[Any]:
    """Build ``contents`` payload for Gemini SDK from an LLMRequest.

    The router / VD pipeline passes images as raw ``bytes`` in
    ``request.contents`` and the text instruction in ``request.prompt``
    (provider-agnostic format, also used by OpenAI-compat providers).

    Gemini's SDK, however, validates the ``contents`` argument against
    ``Content`` / ``Part`` / ``File`` / ``Image`` / ``str`` shapes and
    rejects a bare ``list[bytes]`` with 25-plus Pydantic validation
    errors. This helper converts our generic format into the exact
    SDK-accepted shape:

    - text-only (no contents): pass the prompt string directly
    - already-shaped contents (all items non-bytes): pass through
    - raw bytes list + optional prompt: wrap each bytes item in
      ``Part.from_bytes`` (MIME auto-detected from magic bytes) and
      append the prompt as a text Part, all inside one ``Content``

    Mixing bytes with SDK-native items in the same list is rejected —
    callers should commit to one shape.
    """
    contents = request.contents
    if not contents:
        return request.prompt

    # Single-pass classification with early break once both flags are set.
    has_bytes = False
    has_non_bytes = False
    for c in contents:
        if isinstance(c, (bytes, bytearray)):
            has_bytes = True
        else:
            has_non_bytes = True
        if has_bytes and has_non_bytes:
            break

    # Legacy / SDK-shaped passthrough: e.g. video.py passes
    # types.Content with FileData for the Gemini File API.
    if not has_bytes:
        return contents

    if has_non_bytes:
        msg = (
            "Gemini _build_contents: cannot mix raw bytes with SDK-native "
            "items in the same contents list. Pass either all bytes "
            "(image data) or all SDK-shaped items."
        )
        raise TypeError(msg)

    parts: list[Any] = []
    for item in contents:
        data = bytes(item)
        parts.append(
            types.Part.from_bytes(data=data, mime_type=_detect_image_mime(data))
        )
    if request.prompt:
        parts.append(types.Part.from_text(text=request.prompt))
    return [types.Content(parts=parts)]


class GeminiProvider(LLMProvider):
    """Gemini provider using google-genai SDK.

    Supports text generation and structured output via
    response_mime_type="application/json" + response_schema.

    When multiple API keys are provided, SDK clients are
    pre-created and rotated in round-robin order per request.
    """

    provider_name = "gemini"

    def __init__(self, api_keys: Sequence[str], default_model: str) -> None:
        super().__init__()
        self._clients = tuple(genai.Client(api_key=k) for k in api_keys)
        self._client_cycle: Iterator[genai.Client] = itertools.cycle(self._clients)
        self._default_model = default_model

    def _next_client(self) -> genai.Client:
        return next(self._client_cycle)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Classify google-genai SDK exceptions into ladder categories.

        Mapping:
        * ServerError (5xx) -> INFRASTRUCTURE.
        * ClientError code 408 / 429 -> INFRASTRUCTURE.
        * ClientError code 413 (payload too large) -> INPUT_OVERFLOW.
        * ClientError code 400 with a message matching a known
          context-overflow pattern -> INPUT_OVERFLOW.
        * Other ClientError -> SEMANTIC.
        * Anything else (including raw httpx transport errors that
          bypass the SDK error namespace) -> SEMANTIC (via base).
        """
        if isinstance(exc, genai_errors.ServerError):
            return ErrorCategory.INFRASTRUCTURE
        if isinstance(exc, genai_errors.ClientError):
            if exc.code in (408, 429):
                return ErrorCategory.INFRASTRUCTURE
            if exc.code == 413:
                return ErrorCategory.INPUT_OVERFLOW
            if exc.code == 400 and _GEMINI_OVERFLOW_PATTERN.search(str(exc)):
                return ErrorCategory.INPUT_OVERFLOW
            return ErrorCategory.SEMANTIC
        return super().classify_error(exc)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via Gemini."""
        model = request.model or self._default_model
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
        )

        contents = _build_contents(request)

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        usage = response.usage_metadata
        return LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_token_count if usage else None,
            tokens_out=usage.candidates_token_count if usage else None,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output with native Gemini JSON mode."""
        model = request.model or self._default_model
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        contents = _build_contents(request)

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        usage = response.usage_metadata
        llm_response = LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_token_count if usage else None,
            tokens_out=usage.candidates_token_count if usage else None,
            latency_ms=timer.elapsed_ms,
        )

        parsed = self._parse_structured(response.text or "{}", response_schema)
        return parsed, llm_response
