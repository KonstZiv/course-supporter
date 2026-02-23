"""ARQ worker configuration and lifecycle hooks.

Run with::

    arq course_supporter.worker.WorkerSettings

Or in Docker::

    python -m arq course_supporter.worker.WorkerSettings
"""

from typing import Any, ClassVar

import structlog
from arq.connections import RedisSettings

from course_supporter.api.tasks import arq_generate_structure, arq_ingest_material
from course_supporter.config import get_settings
from course_supporter.logging_config import configure_logging

WorkerCtx = dict[str, Any]


async def startup(ctx: WorkerCtx) -> None:
    """Initialize worker resources on startup.

    Creates an async engine, session factory, and model router,
    storing them in the worker context for use by task functions.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from course_supporter.llm import create_model_router
    from course_supporter.storage.s3 import S3Client

    s = get_settings()
    configure_logging(
        environment=str(s.environment),
        log_level=s.log_level,
    )

    engine = create_async_engine(
        s.database_url,
        pool_size=5,
        max_overflow=10,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    model_router = create_model_router(s, session_factory)

    s3 = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await s3.open()

    ctx["engine"] = engine
    ctx["session_factory"] = session_factory
    ctx["model_router"] = model_router
    ctx["s3_client"] = s3

    log = structlog.get_logger()
    log.info("worker_started", redis_url=s.redis_url, max_jobs=s.worker_max_jobs)


async def shutdown(ctx: WorkerCtx) -> None:
    """Clean up worker resources on shutdown."""
    log = structlog.get_logger()

    s3 = ctx.get("s3_client")
    if s3 is not None:
        await s3.close()

    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()

    log.info("worker_stopped")


class WorkerSettings:
    """ARQ worker settings — consumed by ``arq`` CLI."""

    _settings = get_settings()

    redis_settings: RedisSettings = RedisSettings.from_dsn(
        _settings.redis_url,
    )
    functions: ClassVar[list[Any]] = [arq_ingest_material, arq_generate_structure]
    on_startup = startup
    on_shutdown = shutdown

    max_jobs: int = _settings.worker_max_jobs
    job_timeout: int = _settings.worker_job_timeout
    max_tries: int = _settings.worker_max_tries

    keep_result: int = 3600
    poll_delay: float = 0.5
