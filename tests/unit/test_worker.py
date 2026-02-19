"""Tests for ARQ worker configuration."""

from unittest.mock import MagicMock, patch

from arq.connections import RedisSettings

from course_supporter.worker import WorkerSettings, shutdown, startup


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
        assert WorkerSettings.max_jobs == 2

    def test_job_timeout_default(self) -> None:
        assert WorkerSettings.job_timeout == 1800

    def test_max_tries_default(self) -> None:
        assert WorkerSettings.max_tries == 3

    def test_functions_list(self) -> None:
        assert isinstance(WorkerSettings.functions, list)

    def test_lifecycle_hooks_assigned(self) -> None:
        assert WorkerSettings.on_startup is startup
        assert WorkerSettings.on_shutdown is shutdown

    def test_keep_result(self) -> None:
        assert WorkerSettings.keep_result == 3600

    def test_poll_delay(self) -> None:
        assert WorkerSettings.poll_delay == 0.5


class TestWorkerLifecycle:
    """Startup and shutdown hooks."""

    async def test_startup_configures_logging(self) -> None:
        ctx: dict[str, object] = {}
        with patch("course_supporter.worker.configure_logging") as mock_logging:
            await startup(ctx)
            mock_logging.assert_called_once()

    async def test_shutdown_logs(self) -> None:
        ctx: dict[str, object] = {}
        with patch("course_supporter.worker.structlog") as mock_structlog:
            mock_logger = MagicMock()
            mock_structlog.get_logger.return_value = mock_logger
            await shutdown(ctx)
            mock_logger.info.assert_called_once_with("worker_stopped")
