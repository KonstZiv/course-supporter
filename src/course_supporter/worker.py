"""ARQ worker configuration and lifecycle hooks.

Run with::

    arq course_supporter.worker.WorkerSettings

Or in Docker::

    python -m arq course_supporter.worker.WorkerSettings
"""

from typing import Any, ClassVar

import structlog
from arq.connections import RedisSettings

from course_supporter.config import get_settings
from course_supporter.logging_config import configure_logging

WorkerCtx = dict[str, Any]


async def startup(ctx: WorkerCtx) -> None:
    """Initialize worker resources on startup."""
    s = get_settings()
    configure_logging(
        environment=str(s.environment),
        log_level=s.log_level,
    )
    log = structlog.get_logger()
    log.info("worker_started", redis_url=s.redis_url, max_jobs=s.worker_max_jobs)


async def shutdown(ctx: WorkerCtx) -> None:
    """Clean up worker resources on shutdown."""
    log = structlog.get_logger()
    log.info("worker_stopped")


class WorkerSettings:
    """ARQ worker settings — consumed by ``arq`` CLI."""

    _settings = get_settings()

    redis_settings: RedisSettings = RedisSettings.from_dsn(
        _settings.redis_url,
    )
    functions: ClassVar[list[Any]] = []
    on_startup = startup
    on_shutdown = shutdown

    max_jobs: int = _settings.worker_max_jobs
    job_timeout: int = _settings.worker_job_timeout
    max_tries: int = _settings.worker_max_tries

    keep_result: int = 3600
    poll_delay: float = 0.5
