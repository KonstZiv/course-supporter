"""Tests for ARQ worker configuration."""

import uuid
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from arq.connections import RedisSettings
from structlog.testing import capture_logs

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.config import get_settings
from course_supporter.worker import (
    HomeworkWorkerSettings,
    WorkerSettings,
    _reconcile_orphaned_active_jobs,
    shutdown,
    startup,
)


def _fake_active_job(arq_job_id: str | None) -> MagicMock:
    """Build a stand-in for an ``active`` Job row."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.arq_job_id = arq_job_id
    return job


def _reconcile_harness(
    jobs: list[MagicMock],
    status_by_arq_id: dict[str, object],
) -> tuple[AsyncMock, MagicMock, MagicMock, object]:
    """Wire a mock session_factory + JobRepository + ArqJob.status factory."""
    session = AsyncMock()
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    repo = MagicMock()
    repo.get_active_jobs = AsyncMock(return_value=jobs)
    repo.update_status = AsyncMock()

    def _arq_job(arq_job_id: str, _redis: object) -> MagicMock:
        handle = MagicMock()
        handle.status = AsyncMock(return_value=status_by_arq_id[arq_job_id])
        return handle

    return session, factory, repo, _arq_job


class TestWorkerSettings:
    """WorkerSettings class attributes."""

    def test_redis_settings_type(self) -> None:
        assert isinstance(WorkerSettings.redis_settings, RedisSettings)

    def test_redis_settings_from_config(self) -> None:
        rs = WorkerSettings.redis_settings
        assert rs.host == "localhost"
        assert rs.port == 6379
        assert rs.database == 0

    def test_max_jobs_default(self) -> None:
        assert WorkerSettings.max_jobs == 1

    def test_job_timeout_default(self) -> None:
        assert WorkerSettings.job_timeout == 21600

    def test_max_tries_default(self) -> None:
        assert WorkerSettings.max_tries == 3

    def test_functions_list_contains_arq_ingest(self) -> None:
        assert arq_ingest_material in WorkerSettings.functions

    def test_functions_list_not_empty(self) -> None:
        assert len(WorkerSettings.functions) >= 1

    def test_lifecycle_hooks_assigned(self) -> None:
        assert WorkerSettings.on_startup is startup
        assert WorkerSettings.on_shutdown is shutdown

    def test_keep_result(self) -> None:
        assert WorkerSettings.keep_result == 3600

    def test_poll_delay(self) -> None:
        assert WorkerSettings.poll_delay == 0.5

    def test_expires_extra_ms_overrides_arq_default(self) -> None:
        """expires_extra_ms binds the worker-side pool TTL to settings (ms)."""
        assert WorkerSettings.expires_extra_ms == get_settings().intake_job_expires_ms
        assert WorkerSettings.expires_extra_ms == 345_600_000

    def test_homework_worker_expires_extra_ms_matches(self) -> None:
        """The homework worker pool gets the same override (symmetry)."""
        expected = get_settings().intake_job_expires_ms
        assert HomeworkWorkerSettings.expires_extra_ms == expected
        assert (
            HomeworkWorkerSettings.expires_extra_ms == WorkerSettings.expires_extra_ms
        )


class TestWorkerLifecycle:
    """Startup and shutdown hooks."""

    async def test_startup_configures_logging(self) -> None:
        ctx: dict[str, object] = {}
        with (
            patch("course_supporter.worker.configure_logging") as mock_logging,
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                return_value=MagicMock(),
            ),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker"),
            patch("course_supporter.llm.create_model_router"),
        ):
            await startup(ctx)
            mock_logging.assert_called_once()

    async def test_startup_stores_engine_in_ctx(self) -> None:
        ctx: dict[str, object] = {}
        mock_engine = MagicMock()
        with (
            patch("course_supporter.worker.configure_logging"),
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                return_value=mock_engine,
            ),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker"),
            patch("course_supporter.llm.create_model_router"),
        ):
            await startup(ctx)
        assert ctx["engine"] is mock_engine

    async def test_startup_stores_session_factory_in_ctx(self) -> None:
        ctx: dict[str, object] = {}
        mock_factory = MagicMock()
        with (
            patch("course_supporter.worker.configure_logging"),
            patch("sqlalchemy.ext.asyncio.create_async_engine"),
            patch(
                "sqlalchemy.ext.asyncio.async_sessionmaker",
                return_value=mock_factory,
            ),
            patch("course_supporter.llm.create_model_router"),
        ):
            await startup(ctx)
        assert ctx["session_factory"] is mock_factory

    async def test_startup_stores_model_router_in_ctx(self) -> None:
        ctx: dict[str, object] = {}
        mock_router = MagicMock()
        with (
            patch("course_supporter.worker.configure_logging"),
            patch("sqlalchemy.ext.asyncio.create_async_engine"),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker"),
            patch(
                "course_supporter.llm.create_model_router",
                return_value=mock_router,
            ),
        ):
            await startup(ctx)
        assert ctx["model_router"] is mock_router

    async def test_shutdown_disposes_engine(self) -> None:
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        ctx: dict[str, object] = {"engine": mock_engine}
        with patch("course_supporter.worker.structlog"):
            await shutdown(ctx)
        mock_engine.dispose.assert_awaited_once()

    async def test_shutdown_handles_missing_engine(self) -> None:
        ctx: dict[str, object] = {}
        with patch("course_supporter.worker.structlog") as mock_structlog:
            mock_logger = MagicMock()
            mock_structlog.get_logger.return_value = mock_logger
            await shutdown(ctx)
            mock_logger.info.assert_called_once_with("worker_stopped")

    async def test_shutdown_logs(self) -> None:
        ctx: dict[str, object] = {}
        with patch("course_supporter.worker.structlog") as mock_structlog:
            mock_logger = MagicMock()
            mock_structlog.get_logger.return_value = mock_logger
            await shutdown(ctx)
            mock_logger.info.assert_called_once_with("worker_stopped")

    async def test_startup_creates_s3_client(self) -> None:
        """startup() stores an S3Client in ctx['s3_client']."""
        ctx: dict[str, object] = {}
        mock_s3 = MagicMock()
        mock_s3.open = AsyncMock()
        with (
            patch("course_supporter.worker.configure_logging"),
            patch("sqlalchemy.ext.asyncio.create_async_engine"),
            patch("sqlalchemy.ext.asyncio.async_sessionmaker"),
            patch("course_supporter.llm.create_model_router"),
            patch(
                "course_supporter.storage.s3.S3Client",
                return_value=mock_s3,
            ),
        ):
            await startup(ctx)
        assert ctx["s3_client"] is mock_s3
        mock_s3.open.assert_awaited_once()

    async def test_shutdown_closes_s3_client(self) -> None:
        """shutdown() calls close() on s3_client."""
        mock_s3 = MagicMock()
        mock_s3.close = AsyncMock()
        ctx: dict[str, object] = {"s3_client": mock_s3}
        with patch("course_supporter.worker.structlog"):
            await shutdown(ctx)
        mock_s3.close.assert_awaited_once()


class TestReconcileOrphanedActiveJobs:
    """Startup reconcile of orphaned active jobs (task 3.3c-B, Vector 3)."""

    _JOB_REPO = "course_supporter.storage.job_repository.JobRepository"
    _ARQ_JOB = "arq.jobs.Job"

    async def test_orphaned_failed_and_live_left(self) -> None:
        """not_found/complete → failed; in_progress/queued/deferred → left."""
        from arq.jobs import JobStatus

        not_found = _fake_active_job("a")
        complete = _fake_active_job("b")
        in_progress = _fake_active_job("c")
        queued = _fake_active_job("d")
        deferred = _fake_active_job("e")
        status = {
            "a": JobStatus.not_found,
            "b": JobStatus.complete,
            "c": JobStatus.in_progress,
            "d": JobStatus.queued,
            "e": JobStatus.deferred,
        }
        session, factory, repo, arq_job = _reconcile_harness(
            [not_found, complete, in_progress, queued, deferred], status
        )

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
        ):
            await _reconcile_orphaned_active_jobs(factory, MagicMock())

        failed_ids = {call.args[0] for call in repo.update_status.call_args_list}
        assert failed_ids == {not_found.id, complete.id}
        assert repo.update_status.await_count == 2
        session.commit.assert_awaited_once()

    async def test_missing_arq_job_id_is_reconciled_without_arq_call(self) -> None:
        """A None arq_job_id is treated as orphaned (no ARQ lookup)."""
        job = _fake_active_job(None)
        session, factory, repo, arq_job = _reconcile_harness([job], {})

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job) as arq_patch,
        ):
            await _reconcile_orphaned_active_jobs(factory, MagicMock())

        repo.update_status.assert_awaited_once_with(job.id, "failed", error_message=ANY)
        arq_patch.assert_not_called()
        session.commit.assert_awaited_once()

    async def test_no_active_jobs_is_noop(self) -> None:
        """Empty active set: no status writes, no commit."""
        session, factory, repo, _ = _reconcile_harness([], {})

        with patch(self._JOB_REPO, return_value=repo):
            await _reconcile_orphaned_active_jobs(factory, MagicMock())

        repo.update_status.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_all_live_does_not_commit(self) -> None:
        """When every active job is still live in ARQ, nothing is written."""
        from arq.jobs import JobStatus

        job = _fake_active_job("x")
        session, factory, repo, arq_job = _reconcile_harness(
            [job], {"x": JobStatus.in_progress}
        )

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
        ):
            await _reconcile_orphaned_active_jobs(factory, MagicMock())

        repo.update_status.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_transition_error_is_swallowed_and_others_continue(self) -> None:
        """A ValueError on one transition is logged; the sweep continues."""
        from arq.jobs import JobStatus

        bad = _fake_active_job("a")
        good = _fake_active_job("b")
        status = {"a": JobStatus.not_found, "b": JobStatus.not_found}
        session, factory, repo, arq_job = _reconcile_harness([bad, good], status)
        repo.update_status = AsyncMock(side_effect=[ValueError("race"), None])

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
            capture_logs() as logs,
        ):
            await _reconcile_orphaned_active_jobs(factory, MagicMock())

        assert repo.update_status.await_count == 2
        session.commit.assert_awaited_once()  # the second (good) one reconciled
        assert [e for e in logs if e["event"] == "reconcile_status_skipped"]
