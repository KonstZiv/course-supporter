"""Tests for background ingestion task."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.api.tasks import ingest_material
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


class TestIngestMaterial:
    @pytest.mark.asyncio
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

        # First call: processing, second call: done
        assert mock_repo.update_status.call_count == 2
        calls = mock_repo.update_status.call_args_list
        assert calls[0].args == (material_id, "processing")
        assert calls[1].args == (material_id, "done")

    @pytest.mark.asyncio
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

        # Error repo should transition to error
        assert len(repo_instances) >= 2
        error_repo = repo_instances[-1]
        error_repo.update_status.assert_awaited_once()
        call_args = error_repo.update_status.call_args
        assert call_args.args[1] == "error"
        assert "Processing failed" in str(call_args.kwargs.get("error_message", ""))

    @pytest.mark.asyncio
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

        # Error repo should report invalid source_type
        error_repo = repo_instances[-1]
        error_repo.update_status.assert_awaited_once()
        call_args = error_repo.update_status.call_args
        assert call_args.args[1] == "error"
