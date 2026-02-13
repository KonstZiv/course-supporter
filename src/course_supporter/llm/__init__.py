"""LLM infrastructure: providers, schemas, router, registry, logging.

Quick start::

    from course_supporter.config import get_settings
    from course_supporter.llm import create_model_router

    router = create_model_router(get_settings())
    response = await router.complete("course_structuring", prompt)
"""

from course_supporter.llm.router import AllModelsFailedError, ModelRouter
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.setup import create_model_router

__all__ = [
    "AllModelsFailedError",
    "LLMRequest",
    "LLMResponse",
    "ModelRouter",
    "create_model_router",
]
