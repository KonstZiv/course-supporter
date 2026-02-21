"""Tests for ARQ worker configuration."""

from unittest.mock import AsyncMock, MagicMock, patch

from arq.connections import RedisSettings

from course_supporter.api.tasks import arq_ingest_material
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
