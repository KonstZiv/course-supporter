"""Tests for ARQ worker configuration."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from arq.connections import RedisSettings
from arq.jobs import JobStatus
from structlog.testing import capture_logs

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.config import get_settings
from course_supporter.worker import (
    HomeworkWorkerSettings,
    WorkerSettings,
    _reconcile_orphaned_in_flight_jobs,
    shutdown,
    startup,
)

# An in-flight age comfortably past the reconcile grace window (default seed);
# individual tests override with a fresh queued_at to exercise the grace.
_OLD_QUEUED_AT = datetime.now(UTC) - timedelta(hours=1)


def _fake_active_job(
    arq_job_id: str | None,
    status: str = "active",
    queued_at: datetime | None = None,
) -> MagicMock:
    """Build a stand-in for an in-flight Job row (``active``, old, by default)."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.arq_job_id = arq_job_id
    job.status = status
    job.queued_at = queued_at if queued_at is not None else _OLD_QUEUED_AT
    return job


def _reconcile_harness(
    jobs: list[MagicMock],
    # Mapping (not dict) — covariant in the value type, so a call site can pass
    # a homogeneous ``{id: JobStatus}`` (same status on every queue) OR a
    # ``{id: {queue: JobStatus}}`` (per-queue) literal without an invariance clash.
    status_by_arq_id: Mapping[str, JobStatus | dict[str, JobStatus]],
) -> tuple[AsyncMock, MagicMock, MagicMock, object]:
    """Wire a mock session_factory + JobRepository + ArqJob.status factory.

    A ``status_by_arq_id`` value may be a single status (same on every queue) or
    a ``{queue_name: status}`` dict (per-queue — mirrors arq's per-queue zscore).
    """
    session = AsyncMock()
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    repo = MagicMock()
    repo.get_in_flight_jobs = AsyncMock(return_value=jobs)
    repo.update_status = AsyncMock()

    def _arq_job(
        arq_job_id: str, _redis: object, _queue_name: str = "arq:queue"
    ) -> MagicMock:
        entry = status_by_arq_id[arq_job_id]
        status = entry[_queue_name] if isinstance(entry, dict) else entry
        handle = MagicMock()
        handle.status = AsyncMock(return_value=status)
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
        ):
            await startup(ctx)
        assert ctx["session_factory"] is mock_factory

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
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

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
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        repo.update_status.assert_awaited_once_with(job.id, "failed", error_message=ANY)
        arq_patch.assert_not_called()
        session.commit.assert_awaited_once()

    async def test_no_active_jobs_is_noop(self) -> None:
        """Empty active set: no status writes, no commit."""
        session, factory, repo, _ = _reconcile_harness([], {})

        with patch(self._JOB_REPO, return_value=repo):
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        repo.update_status.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_all_live_does_not_commit(self) -> None:
        """When every active job is still live in ARQ, nothing is written."""

        job = _fake_active_job("x")
        session, factory, repo, arq_job = _reconcile_harness(
            [job], {"x": JobStatus.in_progress}
        )

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
        ):
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        repo.update_status.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_transition_error_is_swallowed_and_others_continue(self) -> None:
        """A ValueError on one transition is logged; the sweep continues."""

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
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        assert repo.update_status.await_count == 2
        session.commit.assert_awaited_once()  # the second (good) one reconciled
        assert [e for e in logs if e["event"] == "reconcile_status_skipped"]

    async def test_queued_orphan_reconciled_live_queued_untouched(self) -> None:
        """L2 F11 / R7: a `queued` Job with a lost ARQ key → failed; a freshly-
        enqueued `queued` Job (ARQ status queued/deferred) is left untouched."""

        orphan = _fake_active_job("lost", status="queued")
        fresh = _fake_active_job("fresh", status="queued")
        status = {"lost": JobStatus.not_found, "fresh": JobStatus.queued}
        session, factory, repo, arq_job = _reconcile_harness([orphan, fresh], status)

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
        ):
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        # Only the lost-key queued orphan is failed; the live one is untouched.
        repo.update_status.assert_awaited_once_with(
            orphan.id, "failed", error_message=ANY
        )
        session.commit.assert_awaited_once()

    async def test_homework_queue_live_key_untouched(self) -> None:
        """R7 / Z-1: a queued Job whose ARQ key is live only on the HOMEWORK
        queue (``not_found`` on the default queue) must NOT be reconciled —
        queued/deferred is per-queue (arq zscore), so both worker queues are
        consulted; an orphan is not-live on BOTH."""

        hw_queue = HomeworkWorkerSettings.queue_name
        live_hw = _fake_active_job("hw", status="queued")
        orphan = _fake_active_job("gone", status="queued")
        status = {
            "hw": {"arq:queue": JobStatus.not_found, hw_queue: JobStatus.queued},
            "gone": {"arq:queue": JobStatus.not_found, hw_queue: JobStatus.not_found},
        }
        session, factory, repo, arq_job = _reconcile_harness([live_hw, orphan], status)

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job),
        ):
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        # Homework-live job untouched; only the double-not_found orphan fails.
        repo.update_status.assert_awaited_once_with(
            orphan.id, "failed", error_message=ANY
        )
        session.commit.assert_awaited_once()

    async def test_fresh_queued_row_within_grace_is_skipped(self) -> None:
        """Z-2: a just-INSERTed queued Job (fresh queued_at, NULL arq_job_id —
        the mid-enqueue window) is NOT reconciled, and no ARQ lookup runs."""
        fresh = _fake_active_job(None, status="queued", queued_at=datetime.now(UTC))
        session, factory, repo, arq_job = _reconcile_harness([fresh], {})

        with (
            patch(self._JOB_REPO, return_value=repo),
            patch(self._ARQ_JOB, side_effect=arq_job) as arq_patch,
        ):
            await _reconcile_orphaned_in_flight_jobs(factory, MagicMock())

        repo.update_status.assert_not_awaited()
        arq_patch.assert_not_called()  # grace-skipped before any ARQ query
        session.commit.assert_not_awaited()
