"""ARQ worker configuration and lifecycle hooks.

Run with::

    arq course_supporter.worker.WorkerSettings

Or in Docker::

    python -m arq course_supporter.worker.WorkerSettings
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

import structlog
from arq.connections import ArqRedis, RedisSettings

from course_supporter.api.tasks import (
    arq_ingest_material,
    arq_process_homework,
    arq_regenerate_node_summary,
)
from course_supporter.config import get_settings
from course_supporter.logging_config import configure_logging
from course_supporter.workers.base_normalize import base_normalize_task
from course_supporter.workers.email_send import arq_send_email
from course_supporter.workers.s3_cleanup import s3_cleanup_task

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WorkerCtx = dict[str, Any]


# Startup-race grace: a Job is enqueued in two steps — INSERT + commit (durable
# Job first, KD13/QQ5), then `redis.enqueue_job`, then set arq_job_id + commit.
# Between the two commits a queued Job carries a NULL arq_job_id transiently. If
# the reconcile sweep runs in that window it must NOT mistake it for an orphan,
# so rows younger than this window are skipped. 60s comfortably covers the two
# commits + the enqueue round-trip while staying far below any real orphan age.
_RECONCILE_GRACE_S = 60


async def _reconcile_orphaned_in_flight_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    redis: ArqRedis,
) -> None:
    """Fail Jobs stranded in flight with no live ARQ handle (task 3.3c-B; L2).

    Runs once at worker startup, before this worker polls any job. Sweeps BOTH
    in-flight statuses (``active`` — a previous worker died mid-task — and
    ``queued`` — enqueued but its ARQ task was lost, e.g. a Redis flush; L2 F11:
    queued orphans were previously invisible forever). For each, consult ARQ for
    the liveness of its ``arq_job_id``:

    * live (``in_progress`` / ``queued`` / ``deferred`` on ANY worker queue) →
      ARQ will (re)run it → leave it. R7: a freshly-enqueued ``queued`` Job has a
      live ARQ key, so it is never reconciled — the same liveness rule that
      protects a running ``active`` job.
    * ``not_found`` / ``complete`` on every queue (or no ``arq_job_id``) → ARQ has
      no live handle → transition the Job to ``failed`` so the single-worker queue
      is unblocked and the stranded job becomes visible instead of a spinner.

    **Per-queue liveness (arq/jobs.py::Job.status):** ``complete`` /
    ``in_progress`` are global keys, but ``queued`` / ``deferred`` come from a
    ``zscore`` on the *queue-specific* sorted set (``self._queue_name``). So a
    homework-queue Job is only visible when queried with the homework queue name.
    We therefore check BOTH worker queues (default + ``HomeworkWorkerSettings``'s)
    and treat a Job as orphaned only when it is not-live on ALL of them — no
    job_type→queue mapping, just the two known worker queues.

    **Startup-race grace:** rows younger than :data:`_RECONCILE_GRACE_S` are
    skipped — their NULL ``arq_job_id`` may be the transient mid-enqueue state,
    not an orphan.
    """
    from arq.constants import default_queue_name
    from arq.jobs import Job as ArqJob
    from arq.jobs import JobStatus

    from course_supporter.storage.job_repository import JobRepository

    log = structlog.get_logger()
    arq_live = {JobStatus.in_progress, JobStatus.queued, JobStatus.deferred}
    queues = (default_queue_name, HomeworkWorkerSettings.queue_name)

    async def _arq_is_live(arq_job_id: str) -> bool:
        """True if ARQ reports a live handle on ANY worker queue (see docstring —
        queued/deferred are per-queue, so both queues must be consulted)."""
        for queue in queues:
            if await ArqJob(arq_job_id, redis, _queue_name=queue).status() in arq_live:
                return True
        return False

    now = datetime.now(UTC)
    grace = timedelta(seconds=_RECONCILE_GRACE_S)

    async with session_factory() as session:
        job_repo = JobRepository(session)
        in_flight = await job_repo.get_in_flight_jobs()
        if not in_flight:
            return

        reconciled = 0
        for job in in_flight:
            if job.queued_at is not None and now - job.queued_at < grace:
                continue  # mid-enqueue window — NULL arq_job_id is transient
            if job.arq_job_id is not None and await _arq_is_live(job.arq_job_id):
                continue
            # No arq_job_id, or ARQ reports not_found / complete on every queue →
            # orphaned. The message names the prior status so an operator can tell
            # a died-mid-task (`active`) apart from a never-picked-up (`queued`).
            reason = (
                f"Reconciled at worker startup: stranded in {job.status!r} with "
                "no live ARQ job "
                + (
                    "(previous worker died mid-task)."
                    if job.status == "active"
                    else "(enqueued task lost — never picked up)."
                )
            )
            try:
                await job_repo.update_status(job.id, "failed", error_message=reason)
                reconciled += 1
            except ValueError as exc:
                log.warning(
                    "reconcile_status_skipped", job_id=str(job.id), reason=str(exc)
                )

        if reconciled:
            await session.commit()
            log.info("orphaned_in_flight_jobs_reconciled", count=reconciled)


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
    from course_supporter.llm.factory import create_providers
    from course_supporter.llm.ladder_config import (
        load_ladder_config,
        validate_ladders_against_registry,
    )
    from course_supporter.llm.registry import load_registry
    from course_supporter.llm.stage_router import StageRouter
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
    # Load the model registry once and share it across both routers
    # (TASK-2.4.22): ModelRouter consumes it for action-chain routing and
    # cost; StageRouter consumes it for ESC cost_usd + max_tokens fallback
    # on unpinned ladder rungs.
    registry = load_registry(s.external_services_path)
    model_router = create_model_router(s, session_factory, registry=registry)

    # KD16 StageRouter — separate provider dict per Phase 1.2 §6.2 ratify
    # (option a, two-build); mirrors the FastAPI lifespan wiring in
    # ``api/app.py`` so worker-side consumers (e.g. ``arq_process_homework``
    # invoking ``run_stage2_safety_check``) can read the same ladder
    # registry as HTTP-side consumers.
    ladder_config = load_ladder_config(s.ladders_dir)
    # Fail-fast on rung-typo / capability-mismatch before the worker
    # starts accepting jobs (TASK-2.4.23 — DD-2.4-K + DD-2.4-Q-axis1).
    validate_ladders_against_registry(ladder_config, registry)
    stage_router_providers = create_providers(s)
    stage_router = StageRouter(
        ladder_config=ladder_config,
        providers=stage_router_providers,
        registry=registry,
        session_factory=session_factory,
    )

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
    ctx["stage_router"] = stage_router
    ctx["s3_client"] = s3

    log = structlog.get_logger()

    # Reconcile Jobs stranded in flight (active OR queued) with no live ARQ
    # handle (task 3.3c-B, Vector 3; L2 extends to queued — F11). Best-effort and
    # guarded: a reconcile failure must not stop the worker from starting to
    # process new jobs. ARQ sets ctx['redis'] before invoking on_startup; ``.get``
    # keeps direct-call tests (which pass a bare ctx) from tripping over a
    # missing pool.
    redis = ctx.get("redis")
    if isinstance(redis, ArqRedis):
        try:
            await _reconcile_orphaned_in_flight_jobs(session_factory, redis)
        except Exception:
            log.exception("orphaned_in_flight_jobs_reconcile_failed")

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
    functions: ClassVar[list[Any]] = [
        arq_ingest_material,
        arq_regenerate_node_summary,
        s3_cleanup_task,
        arq_send_email,
        base_normalize_task,
    ]
    on_startup = startup
    on_shutdown = shutdown

    max_jobs: int = _settings.worker_max_jobs
    job_timeout: int = _settings.worker_job_timeout
    max_tries: int = _settings.worker_max_tries

    # DD-3.3c-I: override ARQ's 24h default pending-job TTL. ARQ's
    # ``get_kwargs`` maps this class attribute onto the worker-side pool
    # (``ctx['redis']``, used by cascade S3-cleanup enqueues per KD13); the
    # API-side pool is wired symmetrically in ``api/app.py``. Unit is
    # milliseconds (centralized hours->ms conversion on Settings).
    expires_extra_ms: int = _settings.intake_job_expires_ms

    keep_result: int = 3600
    poll_delay: float = 0.5

    # ARQ defaults heartbeat to ``job_timeout`` (~6h here); explicit
    # 120s makes the worker recoverable within reasonable bounds while
    # comfortably exceeding a single reasoning-tier LLM await
    # (TASK-2.4.17 observed 149-707s) when the loop is unblocked by
    # the SDK timeout (openai_compat._DEFAULT_HTTP_TIMEOUT, TASK-2.4.18).
    health_check_interval: int = 120


class HomeworkWorkerSettings:
    """ARQ worker settings for the homework queue.

    Run with::

        arq course_supporter.worker.HomeworkWorkerSettings
    """

    _settings = get_settings()

    redis_settings: RedisSettings = RedisSettings.from_dsn(
        _settings.redis_url,
    )
    functions: ClassVar[list[Any]] = [
        arq_process_homework,
    ]
    on_startup = startup
    on_shutdown = shutdown

    queue_name: str = "homework"
    max_jobs: int = _settings.worker_max_jobs
    job_timeout: int = _settings.worker_job_timeout
    max_tries: int = _settings.worker_max_tries

    # DD-3.3c-I: mirror WorkerSettings — override ARQ's 24h default
    # pending-job TTL on the homework worker-side pool too.
    expires_extra_ms: int = _settings.intake_job_expires_ms

    keep_result: int = 3600
    poll_delay: float = 0.5
    health_check_interval: int = 120
