"""FastAPI dependency injection."""

from fastapi import Request

from course_supporter.llm.router import ModelRouter
from course_supporter.storage.database import get_session
from course_supporter.storage.s3 import S3Client

__all__ = ["get_model_router", "get_s3_client", "get_session"]


async def get_model_router(request: Request) -> ModelRouter:
    """Retrieve ModelRouter from app state.

    Initialized during lifespan startup.
    """
    return request.app.state.model_router  # type: ignore[no-any-return]


async def get_s3_client(request: Request) -> S3Client:
    """Retrieve S3Client from app state.

    Initialized during lifespan startup.
    """
    return request.app.state.s3_client  # type: ignore[no-any-return]
