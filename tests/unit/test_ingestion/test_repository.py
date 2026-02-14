"""Tests for SourceMaterialRepository."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.storage.repositories import (
    VALID_TRANSITIONS,
    SourceMaterialRepository,
)


def _make_material(
    status: str = "pending",
    material_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock SourceMaterial ORM object."""
    mat = MagicMock()
    mat.id = material_id or uuid.uuid4()
    mat.course_id = uuid.uuid4()
    mat.source_type = "video"
    mat.source_url = "file:///v.mp4"
    mat.filename = "v.mp4"
    mat.status = status
    mat.content_snapshot = None
    mat.processed_at = None
    mat.error_message = None
    mat.created_at = datetime.now(UTC)
    return mat


class TestCreate:
    async def test_create_material(self) -> None:
        """Adds SourceMaterial to session with pending status."""
        session = AsyncMock()
        session.add = MagicMock()  # add() is sync, not a coroutine
        repo = SourceMaterialRepository(session)

        course_id = uuid.uuid4()
        with patch(
            "course_supporter.storage.repositories.SourceMaterial"
        ) as mock_material_cls:
            mock_instance = MagicMock()
            mock_material_cls.return_value = mock_instance

            result = await repo.create(
                course_id=course_id,
                source_type="video",
                source_url="file:///v.mp4",
                filename="v.mp4",
            )

        session.add.assert_called_once_with(mock_instance)
        session.flush.assert_awaited_once()
        assert result is mock_instance


class TestGetById:
    async def test_found(self) -> None:
        """Returns SourceMaterial when found."""
        mat = _make_material()
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_id(mat.id)

        assert result is mat

    async def test_not_found(self) -> None:
        """Returns None when not found."""
        session = AsyncMock()
        session.get.return_value = None

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_id(uuid.uuid4())

        assert result is None


class TestGetByCourseId:
    async def test_returns_list(self) -> None:
        """Returns list of materials for a course."""
        mat1 = _make_material()
        mat2 = _make_material()

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mat1, mat2]

        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        session = AsyncMock()
        session.execute.return_value = mock_result

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_course_id(uuid.uuid4())

        assert len(result) == 2

    async def test_returns_empty_list(self) -> None:
        """Course with no materials returns empty list."""
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []

        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        session = AsyncMock()
        session.execute.return_value = mock_result

        repo = SourceMaterialRepository(session)
        result = await repo.get_by_course_id(uuid.uuid4())

        assert result == []


class TestUpdateStatus:
    async def test_pending_to_processing(self) -> None:
        """pending → processing OK."""
        mat = _make_material(status="pending")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(mat.id, "processing")

        assert result.status == "processing"
        session.flush.assert_awaited()

    async def test_processing_to_done(self) -> None:
        """processing → done, sets processed_at."""
        mat = _make_material(status="processing")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(mat.id, "done")

        assert result.status == "done"
        assert isinstance(result.processed_at, datetime)

    async def test_processing_to_error(self) -> None:
        """processing → error, sets error_message."""
        mat = _make_material(status="processing")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(
            mat.id, "error", error_message="Something broke"
        )

        assert result.status == "error"
        assert result.error_message == "Something broke"

    async def test_processing_to_error_without_message(self) -> None:
        """processing → error without error_message raises ValueError."""
        mat = _make_material(status="processing")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="error_message is required"):
            await repo.update_status(mat.id, "error")

    async def test_invalid_transition(self) -> None:
        """pending → done raises ValueError."""
        mat = _make_material(status="pending")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="Invalid status transition"):
            await repo.update_status(mat.id, "done")

    async def test_terminal_state_done(self) -> None:
        """done → any raises ValueError."""
        mat = _make_material(status="done")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="Invalid status transition"):
            await repo.update_status(mat.id, "processing")

    async def test_not_found(self) -> None:
        """Non-existent material_id raises ValueError."""
        session = AsyncMock()
        session.get.return_value = None

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="SourceMaterial not found"):
            await repo.update_status(uuid.uuid4(), "processing")

    async def test_content_snapshot_saved(self) -> None:
        """content_snapshot parameter stored on material."""
        mat = _make_material(status="pending")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        result = await repo.update_status(
            mat.id, "processing", content_snapshot="<html>snapshot</html>"
        )

        assert result.content_snapshot == "<html>snapshot</html>"

    async def test_terminal_state_error(self) -> None:
        """error → any raises ValueError."""
        mat = _make_material(status="error")
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="Invalid status transition"):
            await repo.update_status(mat.id, "pending")


class TestDelete:
    async def test_delete_existing(self) -> None:
        """Deletes material from session."""
        mat = _make_material()
        session = AsyncMock()
        session.get.return_value = mat

        repo = SourceMaterialRepository(session)
        await repo.delete(mat.id)

        session.delete.assert_awaited_once_with(mat)
        session.flush.assert_awaited()

    async def test_delete_not_found(self) -> None:
        """Non-existent material_id raises ValueError."""
        session = AsyncMock()
        session.get.return_value = None

        repo = SourceMaterialRepository(session)
        with pytest.raises(ValueError, match="SourceMaterial not found"):
            await repo.delete(uuid.uuid4())


class TestValidTransitions:
    def test_transition_map_completeness(self) -> None:
        """All 4 statuses present in VALID_TRANSITIONS."""
        assert set(VALID_TRANSITIONS.keys()) == {
            "pending",
            "processing",
            "done",
            "error",
        }
