"""Anthropic Claude provider."""

import itertools
import json
import re
from collections.abc import Iterator, Sequence
from typing import Any

import anthropic
from pydantic import BaseModel

from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse


class AnthropicProvider(LLMProvider):
    """Anthropic provider using official SDK.

    When multiple API keys are provided, SDK clients are
    pre-created and rotated in round-robin order per request.
    """

    provider_name = "anthropic"

    # Anthropic API requires max_tokens; used when neither request
    # nor model config specifies a value.
    DEFAULT_MAX_TOKENS = 8192
    default_max_output_tokens: int = DEFAULT_MAX_TOKENS

    def __init__(self, api_keys: Sequence[str], default_model: str) -> None:
        super().__init__()
        self._clients = tuple(anthropic.AsyncAnthropic(api_key=k) for k in api_keys)
        self._client_cycle: Iterator[anthropic.AsyncAnthropic] = itertools.cycle(
            self._clients
        )
        self._default_model = default_model

    def _next_client(self) -> anthropic.AsyncAnthropic:
        return next(self._client_cycle)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Generate text completion via Anthropic."""
        model = request.model or self._default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens or self.DEFAULT_MAX_TOKENS,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt

        client = self._next_client()
        with self._measure_latency() as timer:
            response = await client.messages.create(**kwargs)

        return LLMResponse(
            content=response.content[0].text if response.content else "",
            provider=self.provider_name,
            model_id=model,
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
