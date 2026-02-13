"""OpenAI-compatible provider (OpenAI + DeepSeek)."""

import json
from typing import Any

import openai
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class OpenAICompatProvider(LLMProvider):
    """Provider for OpenAI API and compatible services (DeepSeek).

    DeepSeek uses the same API format with a different base_url.
    """

    def __init__(
        self,
        api_key: str,
        default_model: str,
        provider_name: str = "openai",
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.provider_name = provider_name
        self._default_model = default_model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via OpenAI-compatible API."""
        model = request.model or self._default_model
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        with self._measure_latency() as timer:
            response = await self._client.chat.completions.create(
                model=model,
                # OpenAI SDK expects union of typed message params
                # (ChatCompletionSystemMessageParam | ...), but accepts
                # plain dicts at runtime. Using dicts keeps code simple
                # and avoids coupling to SDK-specific message types.
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
        """Generate structured output via JSON mode."""
        model = request.model or self._default_model
        messages: list[dict[str, str]] = []
        schema_json = json.dumps(
            response_schema.model_json_schema(), ensure_ascii=False
        )
        system = (
            f"{request.system_prompt or ''}\n\n"
            f"Respond ONLY with valid JSON matching this schema:\n{schema_json}"
        )
        messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": request.prompt})

        with self._measure_latency() as timer:
            # call-overload: OpenAI SDK overloads don't match dict-based
            # messages + dict-based response_format simultaneously, but
            # both are accepted at runtime per OpenAI API docs.
            response = await self._client.chat.completions.create(  # type: ignore[call-overload]
                model=model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response_format={"type": "json_object"},
            )

        choice = response.choices[0]
        usage = response.usage
        llm_response = LLMResponse(
            content=choice.message.content or "",
            provider=self.provider_name,
            model_id=model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=timer.elapsed_ms,
        )
        parsed = self._parse_structured(llm_response.content, response_schema)
        return parsed, llm_response
