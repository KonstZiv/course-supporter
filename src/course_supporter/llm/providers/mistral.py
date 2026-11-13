"""Mistral provider — the OpenAI-compatible client with Mistral's seed field.

Mistral's chat API (``POST /v1/chat/completions``) is reached through the
OpenAI SDK like DeepSeek's, and differs from it in one wire fact the router
needs: it documents a reproducibility seed, under its own name.

Seed (read 2026-11-13, https://docs.mistral.ai/api/endpoint/chat, field
``random_seed``): type ``integer|null``, "The seed to use for random sampling.
If set, different calls will generate deterministic results." No range and no
default are stated; no OpenAI-style ``seed`` alias is documented. Not part of
the OpenAI SDK's named arguments, so it goes in ``extra_body``.
"""

from __future__ import annotations

from typing import Any

from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest


class MistralProvider(OpenAICompatProvider):
    """OpenAI-compatible provider that sends Mistral's ``random_seed``."""

    def seed_supported(self, request: LLMRequest) -> bool:
        """Mistral documents ``random_seed`` on every chat request (see module)."""
        return True

    def _seed_body_fields(self, seed: int) -> dict[str, Any]:
        """``{"random_seed": seed}`` — Mistral's name for the seed."""
        return {"random_seed": seed}
