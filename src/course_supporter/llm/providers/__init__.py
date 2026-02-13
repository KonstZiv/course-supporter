"""LLM provider implementations.

PROVIDER_REGISTRY maps provider names (used in models.yaml)
to their implementation classes. To add a new provider:

1. Create a new module in this package (e.g., mistral.py)
2. Implement LLMProvider subclass
3. Add entry to PROVIDER_REGISTRY below

That's it -- no changes to factory.py or router.py needed.
"""

from course_supporter.llm.providers.anthropic import AnthropicProvider
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider

PROVIDER_REGISTRY: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatProvider,
    "deepseek": OpenAICompatProvider,
}

__all__ = [
    "PROVIDER_REGISTRY",
    "AnthropicProvider",
    "GeminiProvider",
    "LLMProvider",
    "OpenAICompatProvider",
]
