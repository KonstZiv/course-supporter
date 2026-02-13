"""Google Gemini provider via google-genai SDK."""

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
    """

    provider_name = "gemini"

    def __init__(self, api_key: str, default_model: str) -> None:
        super().__init__()
        self._client = genai.Client(api_key=api_key)
        self._default_model = default_model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via Gemini."""
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
        )

        with self._measure_latency() as timer:
            response = await self._client.aio.models.generate_content(
                model=self._default_model,
                contents=request.prompt,
                config=config,
            )

        usage = response.usage_metadata
        return LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=self._default_model,
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
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            system_instruction=request.system_prompt,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        with self._measure_latency() as timer:
            response = await self._client.aio.models.generate_content(
                model=self._default_model,
                contents=request.prompt,
                config=config,
            )

        usage = response.usage_metadata
        llm_response = LLMResponse(
            content=response.text or "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=usage.prompt_token_count if usage else None,
            tokens_out=usage.candidates_token_count if usage else None,
            latency_ms=timer.elapsed_ms,
        )

        parsed = self._parse_structured(response.text or "{}", response_schema)
        return parsed, llm_response
