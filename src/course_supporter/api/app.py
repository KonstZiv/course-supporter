"""FastAPI application with lifespan management."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from course_supporter.api.middleware import RequestLoggingMiddleware
from course_supporter.api.routes.courses import router as courses_router
from course_supporter.api.routes.jobs import router as jobs_router
from course_supporter.api.routes.nodes import router as nodes_router
from course_supporter.api.routes.reports import router as reports_router
from course_supporter.auth.rate_limiter import InMemoryRateLimiter
from course_supporter.auth.scopes import rate_limiter
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
        try:
            cleaned = await asyncio.to_thread(limiter.cleanup)
            if cleaned:
                logger.debug("rate_limiter_cleanup", keys_removed=cleaned)
        except Exception:
            logger.exception("rate_limiter_cleanup_error")


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

    # ARQ Redis pool for job enqueue
    arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.arq_redis = arq_redis

    # Start rate limiter cleanup loop
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
    await arq_redis.aclose()
    await engine.dispose()
    logger.info("app_stopped")


app = FastAPI(
    title="Course Supporter",
    description="AI-powered course structuring from learning materials",
    version="0.1.0",
    lifespan=lifespan,
    debug=settings.is_dev,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allowed_methods,
    allow_headers=settings.cors_allowed_headers,
)


HEALTH_CHECK_TIMEOUT = 5.0


@app.get("/health")
async def health() -> JSONResponse:
    """Deep health check — verifies DB and S3 connectivity."""
    checks: dict[str, str] = {}
    overall = "ok"

    # DB check
    try:
        async with async_session() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        checks["db"] = "ok"
    except (TimeoutError, OperationalError, SQLAlchemyError) as e:
        logger.warning("health_check_db_error", error=type(e).__name__)
        checks["db"] = f"error: {type(e).__name__}"
        overall = "degraded"
    except Exception as e:
        logger.error("health_check_db_unexpected", error=str(e), exc_info=True)
        checks["db"] = f"error: {type(e).__name__}"
        overall = "degraded"

    # S3 check
    try:
        s3_client = app.state.s3_client
        await asyncio.wait_for(
            s3_client.check_connectivity(),
            timeout=HEALTH_CHECK_TIMEOUT,
        )
        checks["s3"] = "ok"
    except (TimeoutError, ClientError) as e:
        logger.warning("health_check_s3_error", error=type(e).__name__)
        checks["s3"] = f"error: {type(e).__name__}"
        overall = "degraded"
    except Exception as e:
        logger.error("health_check_s3_unexpected", error=str(e), exc_info=True)
        checks["s3"] = f"error: {type(e).__name__}"
        overall = "degraded"

    # Redis check
    try:
        arq_redis = app.state.arq_redis
        await asyncio.wait_for(
            arq_redis.ping(),
            timeout=HEALTH_CHECK_TIMEOUT,
        )
        checks["redis"] = "ok"
    except (TimeoutError, ConnectionError, OSError) as e:
        logger.warning("health_check_redis_error", error=type(e).__name__)
        checks["redis"] = f"error: {type(e).__name__}"
        overall = "degraded"
    except Exception as e:
        logger.error("health_check_redis_unexpected", error=str(e), exc_info=True)
        checks["redis"] = f"error: {type(e).__name__}"
        overall = "degraded"

    status_code = 200 if overall == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )


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
app.include_router(nodes_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
