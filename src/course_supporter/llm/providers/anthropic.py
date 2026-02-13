"""Anthropic Claude provider."""

import json
import re
from typing import Any

import anthropic
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class AnthropicProvider(LLMProvider):
    """Anthropic provider using official SDK."""

    provider_name = "anthropic"

    def __init__(self, api_key: str, default_model: str) -> None:
        super().__init__()
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model = default_model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via Anthropic."""
        kwargs: dict[str, Any] = {
            "model": self._default_model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt

        with self._measure_latency() as timer:
            response = await self._client.messages.create(**kwargs)

        return LLMResponse(
            content=response.content[0].text if response.content else "",
            provider=self.provider_name,
            model_id=self._default_model,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms=timer.elapsed_ms,
        )

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type[BaseModel],
    ) -> tuple[Any, LLMResponse]:
        """Generate structured output via system prompt with JSON schema."""
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
        raw = _strip_markdown_json(llm_response.content)
        parsed = self._parse_structured(raw, response_schema)
        return parsed, llm_response


_MD_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_markdown_json(text: str) -> str:
    """Strip markdown code fences from JSON response if present."""
    match = _MD_JSON_RE.search(text)
    return match.group(1).strip() if match else text.strip()
