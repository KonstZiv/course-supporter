"""Google Gemini provider via google-genai SDK."""

import itertools
from collections.abc import Iterator, Sequence
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


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

        contents: str | list[Any] = (
            request.contents if request.contents else request.prompt
        )

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

        contents: str | list[Any] = (
            request.contents if request.contents else request.prompt
        )

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
