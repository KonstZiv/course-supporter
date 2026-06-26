"""Tests for ARQ-based background ingestion task."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.models.source import SourceDocument, SourceType

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"
_ENTRY_REPO = (
    "course_supporter.storage.authored_document_repository.AuthoredDocumentRepository"
)
_SUMMARY_REPO = (
    "course_supporter.storage.document_summary_repository.DocumentSummaryRepository"
)
_SEGMENT_REPO = (
    "course_supporter.storage.document_segment_repository.DocumentSegmentRepository"
)


def _default_summary_draft() -> DocumentSummaryDraft:
    """Build a minimal valid DocumentSummaryDraft for mock Pass 2a."""
    return DocumentSummaryDraft(
        title="t",
        description="d",
        main_concepts=[],
        secondary_concepts=[],
    )


@pytest.fixture()
def _bypass_tenant_lookup() -> None:  # type: ignore[misc]
    """Bypass set_tenant_from_job when tests use mock session factories."""
    with patch(
        "course_supporter.api.tasks.set_tenant_from_job",
        new=AsyncMock(),
    ):
        yield


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
    stage_router: MagicMock | None = None,
) -> dict[str, object]:
    """Build an ARQ worker context dict for testing."""
    return {
        "session_factory": factory or _make_session_factory(),
        "model_router": router,
        "stage_router": stage_router or MagicMock(),
        "redis": MagicMock(),
    }


def _mock_processors(
    doc: SourceDocument | None = None,
    *,
    source_type: SourceType = SourceType.WEB,
    summary_draft: DocumentSummaryDraft | None = None,
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a mock processor instance."""
    if doc is None:
        doc = SourceDocument(
            source_type=source_type,
            source_url="https://example.com",
        )
    mock_proc = MagicMock()
    mock_proc.process_raw = AsyncMock(return_value=doc)
    mock_proc.process_macro = AsyncMock(
        return_value=summary_draft or _default_summary_draft(),
    )
    mock_proc.process_detail = AsyncMock(return_value=[])
    return {source_type: mock_proc}


def _failing_processors(
    *,
    error: str = "Processing failed",
    source_type: SourceType = SourceType.WEB,
) -> dict[SourceType, MagicMock]:
    """Create a processors dict with a processor that raises."""
    mock_proc = MagicMock()
    mock_proc.process_raw = AsyncMock(side_effect=RuntimeError(error))
    return {source_type: mock_proc}


def _mock_entry(source_url: str = "https://example.com") -> MagicMock:
    """Create a mock AuthoredDocument."""
    entry = MagicMock()
    entry.source_url = source_url
    return entry


@pytest.fixture()
def _patch_summary_repo() -> None:  # type: ignore[misc]
    """Auto-patch DocumentSummaryRepository so Pass 2a inside arq_ingest_material
    materialises against a mock (no DB) yielding a usable summary instance."""
    with patch(_SUMMARY_REPO) as cls:
        cls.return_value.create = AsyncMock(
            return_value=MagicMock(id=uuid.uuid4()),
        )
        yield


@pytest.fixture()
def _patch_segment_repo() -> None:  # type: ignore[misc]
    """Auto-patch DocumentSegmentRepository so Pass 2b runs without DB.

    ``create_batch`` returns an empty list; mocked ``process_detail``
    already returns ``[]`` so Pass 2b is a no-op end-to-end for these
    legacy tests that pre-date Phase 2.1 C7.
    """
    with patch(_SEGMENT_REPO) as cls:
        cls.return_value.create_batch = AsyncMock(return_value=[])
        yield


@pytest.fixture()
def _patch_stage2_check() -> None:  # type: ignore[misc]
    """Auto-patch Stage 2 to return is_safe=True (Phase 2.1 C6).

    Pass-through verdict so existing test scenarios (which were
    designed against the pre-C6 flow without LLM safety checks)
    continue covering Pass 2a + callback delegation without a real
    LLM call. Scenarios that want to exercise the reject branch
    re-patch ``run_stage2_safety_check`` directly.
    """
    from course_supporter.security.schemas import SafetyResult

    safe = SafetyResult(
        is_safe=True,
        violations=[],
        confidence=0.95,
        reasoning="benign",
    )
    with patch(
        "course_supporter.security.stage2.run_stage2_safety_check",
        new=AsyncMock(return_value=safe),
    ):
        yield


@pytest.fixture()
def _patch_node_repo_for_stage2() -> MagicMock:  # type: ignore[misc]
    """Auto-patch CourseNodeRepository so Stage 2's course_context build
    resolves root/target nodes without real DB queries.
    """
    with patch(
        "course_supporter.storage.course_node_repository.CourseNodeRepository"
    ) as cls:
        node = MagicMock()
        node.title = "Course"
        node.description = "Desc"
        cls.return_value.get_root_for = AsyncMock(return_value=node)
        cls.return_value.get_by_id = AsyncMock(return_value=node)
        yield cls


@pytest.mark.usefixtures(
    "_bypass_tenant_lookup",
    "_patch_summary_repo",
    "_patch_segment_repo",
    "_patch_stage2_check",
    "_patch_node_repo_for_stage2",
)
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
            mock_job_repo_cls.return_value.update_stage = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_entry_cls.return_value.store_safety_result = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

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
            mock_job.update_stage = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_entry_cls.return_value.store_safety_result = AsyncMock()
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
            mock_job_cls.return_value.update_stage = AsyncMock()
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

    async def test_entry_not_found_returns_early(self) -> None:
        """When AuthoredDocument not found, returns early without processing."""
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
            mock_job_cls.return_value.update_stage = AsyncMock()
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

        original_s3_url = "http://localhost:9000/course-materials/courses/f.md"
        mock_entry_obj = MagicMock()
        mock_entry_obj.source_url = original_s3_url

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
        mock_proc.process_raw = capture_process
        mock_proc.process_macro = AsyncMock(return_value=_default_summary_draft())
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
            mock_job_cls.return_value.update_stage = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=mock_entry_obj
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_entry_cls.return_value.store_safety_result = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx,
                job_id,
                material_id,
                "text",
                "http://localhost:9000/course-materials/courses/f.md",
            )

        mock_s3.download_file.assert_awaited_once_with("courses/f.md")
        assert captured_url == str(tmp)
        # ORM object source_url must remain unchanged (proxy, not mutation)
        assert mock_entry_obj.source_url == original_s3_url

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
            mock_job_cls.return_value.update_stage = AsyncMock()
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

    async def test_presentation_persists_slides_on_seam(self) -> None:
        """Phase 6 T3: presentation ingest persists slides + records keys."""
        from course_supporter.ingestion.presentation import (
            PresentationProcessor,
            SlideRaw,
        )

        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())
        mid = uuid.UUID(material_id)

        doc = SourceDocument(
            source_type=SourceType.PRESENTATION,
            source_url="http://localhost:9000/bucket/deck.pdf",
        )
        slides = [
            SlideRaw(slide_number=1, raw_text="a", image_bytes=b"\x89PNG"),
            SlideRaw(slide_number=2, raw_text="b", image_bytes=b"\x89PNG"),
        ]
        # spec=PresentationProcessor → isinstance(...) is True at the seam.
        pres_proc = MagicMock(spec=PresentationProcessor)
        pres_proc.process_raw = AsyncMock(return_value=doc)
        pres_proc.process_macro = AsyncMock(return_value=_default_summary_draft())
        pres_proc.process_detail = AsyncMock(return_value=[])
        pres_proc.rendered_slides = slides

        ctx = _make_arq_ctx()
        ctx["s3_client"] = AsyncMock()
        persist_keys = ["tenants/t/nodes/n/slides/d/0001.webp", "…/0002.webp"]

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
            patch(_FACTORY, return_value={SourceType.PRESENTATION: pres_proc}),
            patch(
                "course_supporter.ingestion.slide_persist.persist_slide_webps",
                new=AsyncMock(return_value=persist_keys),
            ) as mock_persist,
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_job_cls.return_value.update_stage = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_entry_cls.return_value.store_safety_result = AsyncMock()
            mock_entry_cls.return_value.set_slide_keys = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx,
                job_id,
                material_id,
                "presentation",
                "http://localhost:9000/bucket/deck.pdf",
            )

        mock_persist.assert_awaited_once()
        assert mock_persist.call_args.kwargs["slides"] is slides
        mock_entry_cls.return_value.set_slide_keys.assert_awaited_once_with(
            mid, slide_keys=persist_keys
        )
        mock_cb_cls.return_value.on_success.assert_awaited_once()

    async def test_non_presentation_skips_slide_persist(self) -> None:
        """Non-presentation ingest never touches the slide-persist seam."""
        job_id = str(uuid.uuid4())
        material_id = str(uuid.uuid4())

        doc = SourceDocument(
            source_type=SourceType.WEB, source_url="https://example.com"
        )
        ctx = _make_arq_ctx()
        ctx["s3_client"] = AsyncMock()  # s3 present — yet still skipped

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
            patch(
                "course_supporter.ingestion.slide_persist.persist_slide_webps",
                new=AsyncMock(),
            ) as mock_persist,
        ):
            mock_job_cls.return_value.update_status = AsyncMock()
            mock_job_cls.return_value.update_stage = AsyncMock()
            mock_entry_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_entry()
            )
            mock_entry_cls.return_value.set_pending = AsyncMock()
            mock_entry_cls.return_value.store_safety_result = AsyncMock()
            mock_entry_cls.return_value.set_slide_keys = AsyncMock()
            mock_cb_cls.return_value.on_success = AsyncMock()
            mock_cb_cls.return_value.on_failure = AsyncMock()

            await arq_ingest_material(
                ctx, job_id, material_id, "web", "https://example.com"
            )

        mock_persist.assert_not_awaited()
        mock_entry_cls.return_value.set_slide_keys.assert_not_awaited()
