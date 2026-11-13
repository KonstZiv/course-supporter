"""What a stage puts on each provider's wire, captured through the real router.

A test helper, not a test module: it runs :meth:`StageRouter.execute_stage`
over the real connector classes of ``PROVIDER_REGISTRY`` and stops every call
at the transport — ``httpx`` for the OpenAI-compatible connectors, ``aiohttp``
for DashScope — answering with a canned success. What it returns is the request
body exactly as the SDK serialised it. Gemini is captured at its SDK call (the
``generate_content`` keyword arguments), since its client picks its own
transport.

Used by the locks of the ``deterministic`` flag
(``test_deterministic_seed.py``): the body of a ``deterministic: false`` stage
is fingerprinted and compared with the fingerprint taken on ``main`` before the
flag reached the router.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx

from course_supporter.llm.factory import PROVIDER_CONFIGS
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry, StageConfig
from course_supporter.llm.prompt_loader_md import StagePrompt
from course_supporter.llm.providers import PROVIDER_REGISTRY
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import (
    Capability,
    ModelRegistryConfig,
    ProviderConfig,
    ProviderModelConfig,
)
from course_supporter.llm.stage_router import StageRouter

STAGE_NAME = "criteria_evaluation"
PROMPT = StagePrompt(system="Answer in JSON.", user="Is the work done?")
ANSWER = '{"ok": true}'
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

# The rungs of the path stages (config/submission_paths.yaml), each with the
# capabilities its model declares in the registry. A synthetic registry, so a
# registry edit cannot move a fingerprint.
RUNGS: dict[str, tuple[str, list[Capability]]] = {
    "mistral": ("mistral-small-latest", [Capability.JSON_MODE]),
    "deepseek": ("deepseek-flash", [Capability.JSON_MODE]),
    "deepseek_thinking": ("deepseek-v4-pro", [Capability.JSON_MODE]),
    "dashscope": (
        "qwen3.7-max",
        [Capability.JSON_MODE, Capability.SCHEMA_STRICT],
    ),
    "gemini": (
        "gemini-3.5-flash-lite",
        [Capability.JSON_MODE, Capability.SCHEMA_STRICT],
    ),
}


def registry() -> ModelRegistryConfig:
    return ModelRegistryConfig(
        providers={
            name: ProviderConfig(
                type="llm",
                models=[ProviderModelConfig(id=model, capabilities=caps)],
            )
            for name, (model, caps) in RUNGS.items()
        },
        actions={},
    )


def stage(provider: str, **flags: Any) -> StageConfig:
    """A one-rung stage on ``provider``; ``flags`` are extra StageConfig fields."""
    model, _ = RUNGS[provider]
    return StageConfig(
        prompt_ref="prompts/example/v1.md",
        requires=[Capability.JSON_MODE],
        ladder=[LadderEntry(provider=provider, model=model, max_output_tokens=512)],
        **flags,
    )


def connector(name: str) -> LLMProvider:
    """The production connector class for ``name``, built as the factory does."""
    kwargs: dict[str, Any] = {"api_keys": ["k"], "default_model": "m"}
    kwargs.update(PROVIDER_CONFIGS[name].extra_kwargs)
    if name == "gemini":
        with patch("course_supporter.llm.providers.gemini.genai.Client"):
            return PROVIDER_REGISTRY[name](**kwargs)
    return PROVIDER_REGISTRY[name](**kwargs)


# ── transports ──


def _openai_reply(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ANSWER},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


class _AiohttpReply:
    """The part of ``aiohttp.ClientResponse`` the DashScope SDK reads."""

    status = 200
    content_type = "application/json"

    async def json(self) -> dict[str, Any]:
        return {
            "request_id": "r",
            "output": {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": ANSWER},
                    }
                ]
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def __aenter__(self) -> _AiohttpReply:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _gemini_client(sent: list[bytes]) -> Any:
    async def generate_content(**kwargs: Any) -> Any:
        document = {
            "model": kwargs["model"],
            "contents": kwargs["contents"],
            "config": kwargs["config"].model_dump(mode="json", exclude_none=True),
        }
        sent.append(json.dumps(document, sort_keys=True).encode("utf-8"))
        response = MagicMock()
        response.text = ANSWER
        response.candidates = []
        response.usage_metadata = MagicMock(
            prompt_token_count=1,
            candidates_token_count=1,
            thoughts_token_count=None,
            cached_content_token_count=None,
        )
        return response

    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=generate_content)
    return client


async def run_and_capture(
    provider_name: str,
    stage_config: StageConfig,
    *,
    with_schema: bool,
    with_register: bool = False,
) -> tuple[list[bytes], LLMProvider]:
    """Run one stage through the router; return each request body it sent.

    ``with_register`` hands the router a session factory, so it writes its
    register rows through ``stage_router._persist`` — which the caller patches.
    """
    provider = connector(provider_name)
    router = StageRouter(
        LadderConfig(),
        {provider_name: provider},
        registry=registry(),
        session_factory=AsyncMock() if with_register else None,
    )
    sent: list[bytes] = []

    async def httpx_send(
        _client: httpx.AsyncClient, request: httpx.Request, **_: Any
    ) -> httpx.Response:
        sent.append(request.content)
        return _openai_reply(request)

    async def aiohttp_request(
        _session: aiohttp.ClientSession, *_: Any, **kwargs: Any
    ) -> _AiohttpReply:
        # aiohttp serialises ``json=`` with ``json.dumps`` by default.
        sent.append(json.dumps(kwargs["json"]).encode("utf-8"))
        return _AiohttpReply()

    if provider_name == "gemini":
        provider._client_cycle = itertools.cycle([_gemini_client(sent)])  # type: ignore[attr-defined]

    kwargs: dict[str, Any] = {"expects_json": True}
    if with_schema:
        kwargs["response_schema"] = SCHEMA
    with (
        patch(
            "course_supporter.llm.stage_router.load_prompt",
            lambda prompt_ref, *, base_path=None: PROMPT,
        ),
        patch.object(httpx.AsyncClient, "send", httpx_send),
        patch.object(aiohttp.ClientSession, "_request", aiohttp_request),
    ):
        await router.execute_stage(stage_config, STAGE_NAME, **kwargs)
    return sent, provider


def fingerprint(bodies: list[bytes]) -> str:
    """SHA-256 of the request bodies, in order."""
    digest = hashlib.sha256()
    for body in bodies:
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()
