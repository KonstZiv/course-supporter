"""Tests for background ingestion tasks (legacy and ARQ)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.api.tasks import arq_ingest_material, ingest_material
from course_supporter.models.source import SourceDocument, SourceType


def _make_session_factory(session: AsyncMock | None = None) -> MagicMock:
    """Create a mock async_sessionmaker that returns an async ctx manager.

    async_sessionmaker() returns a context manager directly (not a coroutine),
    so we use MagicMock with __aenter__/__aexit__.
    """
    if session is None:
        session = AsyncMock()
        session.add = MagicMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return factory


def _make_two_session_factory(
    main_session: AsyncMock, error_session: AsyncMock
) -> MagicMock:
    """Create factory returning different sessions for main and error paths."""
    call_count = 0

    def make_ctx() -> MagicMock:
        nonlocal call_count
        call_count += 1
        ctx = MagicMock()
        if call_count <= 1:
            ctx.__aenter__ = AsyncMock(return_value=main_session)
        else:
            ctx.__aenter__ = AsyncMock(return_value=error_session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    return MagicMock(side_effect=make_ctx)


def _make_arq_ctx(
    factory: MagicMock | None = None,
    router: MagicMock | None = None,
) -> dict[str, object]:
    """Build an ARQ worker context dict for testing."""
    return {
        "session_factory": factory or _make_session_factory(),
        "model_router": router,
    }


class TestIngestMaterial:
    """Tests for the legacy ingest_material function."""

    async def test_ingestion_success(self) -> None:
        """ingest_material() processes and transitions to done."""
        material_id = uuid.uuid4()
        doc = SourceDocument(
            source_type=SourceType.WEB,
            source_url="https://example.com",
        )
        mock_material = MagicMock()
        mock_material.source_url = "https://example.com"
        mock_material.source_type = SourceType.WEB

        mock_processor = MagicMock()
        mock_processor.return_value.process = AsyncMock(return_value=doc)

        factory = _make_session_factory()

        with (
            patch(
                "course_supporter.api.tasks.SourceMaterialRepository"
            ) as mock_repo_cls,
            patch(
                "course_supporter.api.tasks.PROCESSOR_MAP",
                {SourceType.WEB: mock_processor},
            ),
        ):
            mock_repo = mock_repo_cls.return_value
            mock_repo.update_status = AsyncMock()
            mock_repo.get_by_id = AsyncMock(return_value=mock_material)

            await ingest_material(
                material_id,
                "web",
                "https://example.com",
                factory,
            )

        assert mock_repo.update_status.call_count == 2
        calls = mock_repo.update_status.call_args_list
        assert calls[0].args == (material_id, "processing")
        assert calls[1].args == (material_id, "done")

    async def test_ingestion_error_sets_error_status(self) -> None:
        """ingest_material() transitions to error on failure."""
        material_id = uuid.uuid4()

        main_session = AsyncMock()
        main_session.add = MagicMock()
        error_session = AsyncMock()
        error_session.add = MagicMock()

        mock_material = MagicMock()
        mock_material.source_url = "https://example.com"
        mock_material.source_type = SourceType.WEB

        mock_processor = MagicMock()
        mock_processor.return_value.process = AsyncMock(
            side_effect=RuntimeError("Processing failed")
        )

        factory = _make_two_session_factory(main_session, error_session)

        with (
            patch(
                "course_supporter.api.tasks.SourceMaterialRepository"
            ) as mock_repo_cls,
            patch(
                "course_supporter.api.tasks.PROCESSOR_MAP",
                {SourceType.WEB: mock_processor},
            ),
        ):
            repo_instances: list[MagicMock] = []

            def make_repo(session: object) -> MagicMock:
                repo = MagicMock()
                repo.update_status = AsyncMock()
                repo.get_by_id = AsyncMock(return_value=mock_material)
                repo_instances.append(repo)
                return repo

            mock_repo_cls.side_effect = make_repo

            await ingest_material(
                material_id,
                "web",
                "https://example.com",
                factory,
            )

        assert len(repo_instances) >= 2
        error_repo = repo_instances[-1]
        error_repo.update_status.assert_awaited_once()
        call_args = error_repo.update_status.call_args
        assert call_args.args[1] == "error"
        assert "Processing failed" in str(call_args.kwargs.get("error_message", ""))

    async def test_ingestion_invalid_source_type(self) -> None:
        """ingest_material() fails for unknown source_type."""
        material_id = uuid.uuid4()

        main_session = AsyncMock()
        main_session.add = MagicMock()
        error_session = AsyncMock()
        error_session.add = MagicMock()

        factory = _make_two_session_factory(main_session, error_session)

        with patch(
            "course_supporter.api.tasks.SourceMaterialRepository"
        ) as mock_repo_cls:
            repo_instances: list[MagicMock] = []

            def make_repo(session: object) -> MagicMock:
                repo = MagicMock()
                repo.update_status = AsyncMock()
                repo_instances.append(repo)
                return repo

            mock_repo_cls.side_effect = make_repo

            await ingest_material(
                material_id,
                "invalid",
                "https://example.com",
                factory,
            )

        error_repo = repo_instances[-1]
        error_repo.update_status.assert_awaited_once()
        call_args = error_repo.update_status.call_args
        assert call_args.args[1] == "error"


class TestArqIngestMaterial:
    """Tests for the ARQ-based arq_ingest_material function."""

    async def test_calls_check_work_window(self) -> None:
        """arq_ingest_material calls check_work_window with correct priority."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        factory = _make_session_factory()
        ctx = _make_arq_ctx(factory=factory)

        with (
            patch("course_supporter.job_priority.check_work_window") as mock_check,
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_repo_cls,
            patch(
                "course_supporter.api.tasks.SourceMaterialRepository"
            ) as mock_mat_repo_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(
                "course_supporter.api.tasks.PROCESSOR_MAP",
                {
                    SourceType.WEB: MagicMock(
                        return_value=MagicMock(
                            process=AsyncMock(
                                return_value=SourceDocument(
                                    source_type=SourceType.WEB,
                                    source_url="https://example.com",
                                )
                            )
                        )
                    )
                },
            ),
        ):
            mock_job_repo_cls.return_value.update_status = AsyncMock()
            mock_mat_repo_cls.return_value.update_status = AsyncMock()
            mock_mat_repo_cls.return_value.get_by_id = AsyncMock(
                return_value=MagicMock()
            )
            mock_cb_cls.return_value.on_success = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com", "immediate"
            )

        from course_supporter.job_priority import JobPriority

        mock_check.assert_called_once_with(JobPriority.IMMEDIATE)

    async def test_success_delegates_to_callback(self) -> None:
        """On success: job activated, then callback.on_success called."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        mid = uuid.UUID(material_id)
        factory = _make_session_factory()
        ctx = _make_arq_ctx(factory=factory)

        doc = SourceDocument(
            source_type=SourceType.WEB, source_url="https://example.com"
        )
        mock_processor = MagicMock()
        mock_processor.return_value.process = AsyncMock(return_value=doc)

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(
                "course_supporter.api.tasks.SourceMaterialRepository"
            ) as mock_mat_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(
                "course_supporter.api.tasks.PROCESSOR_MAP",
                {SourceType.WEB: mock_processor},
            ),
        ):
            mock_job = mock_job_cls.return_value
            mock_job.update_status = AsyncMock()
            mock_mat = mock_mat_cls.return_value
            mock_mat.update_status = AsyncMock()
            mock_mat.get_by_id = AsyncMock(return_value=MagicMock())
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        # Job activated in main session
        mock_job.update_status.assert_awaited_once_with(uuid.UUID(job_id), "active")
        # Material set to processing in main session
        mock_mat.update_status.assert_awaited_once_with(mid, "processing")
        # Completion delegated to callback
        mock_cb_cls.return_value.on_success.assert_awaited_once()
        call_kwargs = mock_cb_cls.return_value.on_success.call_args.kwargs
        assert call_kwargs["job_id"] == uuid.UUID(job_id)
        assert call_kwargs["material_id"] == mid

    async def test_error_delegates_to_callback(self) -> None:
        """On error: session rolled back, callback.on_failure called."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        jid = uuid.UUID(job_id)
        mid = uuid.UUID(material_id)

        session = AsyncMock()
        session.add = MagicMock()
        factory = _make_session_factory(session)
        ctx = _make_arq_ctx(factory=factory)

        mock_processor = MagicMock()
        mock_processor.return_value.process = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(
                "course_supporter.api.tasks.SourceMaterialRepository"
            ) as mock_mat_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(
                "course_supporter.api.tasks.PROCESSOR_MAP",
                {SourceType.WEB: mock_processor},
            ),
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_mat_cls.return_value.update_status = AsyncMock()
            mock_mat_cls.return_value.get_by_id = AsyncMock(return_value=MagicMock())
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        # Main session rolled back
        session.rollback.assert_awaited_once()
        # Failure delegated to callback
        mock_cb_cls.return_value.on_failure.assert_awaited_once()
        call_kwargs = mock_cb_cls.return_value.on_failure.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert "boom" in call_kwargs["error_message"]

    async def test_retry_on_closed_window(self) -> None:
        """NORMAL priority outside window raises arq.Retry."""
        from arq import Retry

        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        ctx = _make_arq_ctx()

        with (
            patch(
                "course_supporter.job_priority.check_work_window",
                side_effect=Retry(defer=3600.0),
            ),
            pytest.raises(Retry),
        ):
            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com", "normal"
            )
