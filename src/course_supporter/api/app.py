"""FastAPI application with lifespan management."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from course_supporter.api.middleware import RequestLoggingMiddleware
from course_supporter.api.routes.courses import router as courses_router
from course_supporter.api.routes.reports import router as reports_router
from course_supporter.auth.rate_limiter import InMemoryRateLimiter
from course_supporter.config import settings
from course_supporter.llm import create_model_router
from course_supporter.logging_config import configure_logging
from course_supporter.storage.database import async_session, engine
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

CLEANUP_INTERVAL_SECONDS = 300


async def _cleanup_loop(limiter: InMemoryRateLimiter) -> None:
    """Periodic cleanup of expired rate limit entries."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cleaned = limiter.cleanup()
        if cleaned:
            logger.debug("rate_limiter_cleanup", keys_removed=cleaned)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown.

    Startup:
        - Create ModelRouter with DB logging enabled.
        - Start rate limiter cleanup task.
    Shutdown:
        - Cancel cleanup task.
        - Dispose database engine (close connection pool).
    """
    configure_logging(
        environment=str(settings.environment),
        log_level=settings.log_level,
    )
    app.state.model_router = create_model_router(settings, async_session)

    # Start rate limiter cleanup loop
    from course_supporter.auth.scopes import rate_limiter

    cleanup_task = asyncio.create_task(_cleanup_loop(rate_limiter))

    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
    )
    async with s3:
        await s3.ensure_bucket()
        app.state.s3_client = s3

        logger.info("app_started", environment=str(settings.environment))
        yield

    cleanup_task.cancel()
    await engine.dispose()
    logger.info("app_stopped")


app = FastAPI(
    title="Course Supporter",
    description="AI-powered course structuring from learning materials",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all handler for unhandled exceptions."""
    logger.error("unhandled_exception", exc_info=exc, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.include_router(courses_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
