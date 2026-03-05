"""Tests for ARQ-based background ingestion task."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.models.source import SourceDocument, SourceType

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"
_ENTRY_REPO = (
    "course_supporter.storage.material_entry_repository.MaterialEntryRepository"
)


def _make_session_factory(session: AsyncMock | None = None) -> MagicMock:
    """Create a mock async_sessionmaker that returns an async ctx manager."""
    if session is None:
        session = AsyncMock()
        session.add = MagicMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return factory


def _make_arq_ctx(
    factory: MagicMock | None = None,
    router: MagicMock | None = None,
) -> dict[str, object]:
    """Build an ARQ worker context dict for testing."""
    return {
        "session_factory": factory or _make_session_factory(),
        "model_router": router,
    }


def _mock_processors(
    doc: SourceDocument | None = None,
    *,
    source_type: SourceType = SourceType.WEB,
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a mock processor instance."""
    if doc is None:
        doc = SourceDocument(
            source_type=source_type,
            source_url="https://example.com",
        )
    mock_proc = MagicMock()
    mock_proc.process = AsyncMock(return_value=doc)
    return {source_type: mock_proc}


def _failing_processors(
    *,
    error: str = "Processing failed",
    source_type: SourceType = SourceType.WEB,
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a processor that raises."""
    mock_proc = MagicMock()
    mock_proc.process = AsyncMock(side_effect=RuntimeError(error))
    return {source_type: mock_proc}


def _mock_entry(source_url: str = "https://example.com") -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.source_url = source_url
    return entry


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
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(_FACTORY, return_value=_mock_processors()),
        ):
            mock_job_repo_cls.return_value.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()

            await arq_ingest_material(
                ctx,
                job_id,
                material_id,
                "web",
                "https://example.com",
                "immediate",
            )

        from course_supporter.job_priority import JobPriority

        mock_check.assert_called_once_with(JobPriority.IMMEDIATE)

    async def test_success_delegates_to_callback(self) -> None:
        """On success: job activated, set_pending called, callback.on_success called."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        jid = uuid.UUID(job_id)
        mid = uuid.UUID(material_id)
        factory = _make_session_factory()
        ctx = _make_arq_ctx(factory=factory)

        doc = SourceDocument(
            source_type=SourceType.WEB, source_url="https://example.com"
        )

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(_FACTORY, return_value=_mock_processors(doc)),
        ):
            mock_job = mock_job_cls.return_value
            mock_job.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        mock_job.update_status.assert_awaited_once_with(jid, "active")
        mock_entry_cls.return_value.set_pending.assert_awaited_once_with(mid, jid)
        mock_cb_cls.return_value.on_success.assert_awaited_once()
        call_kwargs = mock_cb_cls.return_value.on_success.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert call_kwargs["is_new_model"] is True

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

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(
                _FACTORY,
                return_value=_failing_processors(error="boom"),
            ),
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        session.rollback.assert_awaited_once()
        mock_cb_cls.return_value.on_failure.assert_awaited_once()
        call_kwargs = mock_cb_cls.return_value.on_failure.call_args.kwargs
        assert call_kwargs["job_id"] == jid
        assert call_kwargs["material_id"] == mid
        assert "boom" in call_kwargs["error_message"]
        assert call_kwargs["is_new_model"] is True

    async def test_entry_not_found_returns_early(self) -> None:
        """When MaterialEntry not found, returns early without processing."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        factory = _make_session_factory()
        ctx = _make_arq_ctx(factory=factory)

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(_FACTORY, return_value=_mock_processors()),
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(return_value=None)
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        mock_cb_cls.return_value.on_success.assert_not_awaited()
        mock_cb_cls.return_value.on_failure.assert_not_awaited()

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
                ctx,
                job_id,
                material_id,
                "web",
                "https://example.com",
                "normal",
            )

    async def test_s3_url_resolved_before_processing(self) -> None:
        """S3 URL is downloaded and material.source_url is replaced."""
        from pathlib import Path

        import anyio

        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        factory = _make_session_factory()

        mock_entry_obj = MagicMock()
        mock_entry_obj.source_url = (
            "http://localhost:9000/course-materials/courses/f.md"
        )

        mock_s3 = MagicMock()
        mock_s3.extract_key = MagicMock(return_value="courses/f.md")
        tmp = Path("/tmp/test-downloaded.md")
        mock_s3.download_file = AsyncMock(return_value=tmp)

        ctx = _make_arq_ctx(factory=factory)
        ctx["s3_client"] = mock_s3

        captured_url: str | None = None

        async def capture_process(mat: object, **kw: object) -> SourceDocument:
            nonlocal captured_url
            captured_url = mat.source_url  # type: ignore[union-attr]
            return SourceDocument(
                source_type=SourceType.TEXT,
                source_url="http://localhost:9000/course-materials/courses/f.md",
            )

        mock_proc = MagicMock()
        mock_proc.process = capture_process
        procs = {SourceType.TEXT: mock_proc}

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(_FACTORY, return_value=procs),
            patch.object(anyio.Path, "exists", AsyncMock(return_value=False)),
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=mock_entry_obj
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()

            await arq_ingest_material(
                ctx,
                job_id,
                material_id,
                "text",
                "http://localhost:9000/course-materials/courses/f.md",
            )

        mock_s3.download_file.assert_awaited_once_with("courses/f.md")
        assert captured_url == str(tmp)

    async def test_temp_file_cleaned_up_on_success_and_error(self) -> None:
        """Temp file from S3 download is unlinked in finally block."""
        from pathlib import Path

        import anyio

        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        factory = _make_session_factory()

        mock_entry_obj = MagicMock()
        mock_entry_obj.source_url = (
            "http://localhost:9000/course-materials/courses/f.md"
        )

        tmp = Path("/tmp/test-cleanup.md")
        mock_s3 = MagicMock()
        mock_s3.extract_key = MagicMock(return_value="courses/f.md")
        mock_s3.download_file = AsyncMock(return_value=tmp)

        ctx = _make_arq_ctx(factory=factory)
        ctx["s3_client"] = mock_s3

        mock_exists = AsyncMock(return_value=True)
        mock_unlink = AsyncMock()

        with (
            patch("course_supporter.job_priority.check_work_window"),
            patch(
                "course_supporter.storage.job_repository.JobRepository"
            ) as mock_job_cls,
            patch(_ENTRY_REPO) as mock_entry_cls,
            patch(
                "course_supporter.ingestion_callback.IngestionCallback"
            ) as mock_cb_cls,
            patch(_HEAVY),
            patch(
                _FACTORY,
                return_value=_failing_processors(
                    error="boom", source_type=SourceType.TEXT
                ),
            ),
            patch.object(anyio.Path, "exists", mock_exists),
            patch.object(anyio.Path, "unlink", mock_unlink),
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=mock_entry_obj
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx,
                job_id,
                material_id,
                "text",
                "http://localhost:9000/course-materials/courses/f.md",
            )

        mock_unlink.assert_awaited_once_with(missing_ok=True)
