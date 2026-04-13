"""Google Gemini provider via google-genai SDK."""

import itertools
from collections.abc import Iterator, Sequence
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


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
    - already-shaped contents (Content / Part / str items): pass through
    - raw bytes + prompt: wrap bytes in ``Part.from_bytes`` (image/jpeg)
      and append the prompt as a text part, all inside one ``Content``
    """
    contents = request.contents
    if not contents:
        return request.prompt

    # If caller already built SDK-native items (strings, Content, Part,
    # File, Image, dict), pass through unchanged to preserve existing
    # callers (e.g. video.py uses types.Content directly for video URIs).
    if all(not isinstance(c, (bytes, bytearray)) for c in contents):
        return contents

    parts: list[Any] = []
    for item in contents:
        if isinstance(item, (bytes, bytearray)):
            parts.append(
                types.Part.from_bytes(data=bytes(item), mime_type="image/jpeg")
            )
        else:
            # Unknown item type inside a mixed list — trust SDK to handle it.
            parts.append(item)
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
