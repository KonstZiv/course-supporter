"""Tests for POST /courses/{id}/slide-mapping and SlideVideoMappingRepository."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.storage.database import get_session
from course_supporter.storage.repositories import (
    CourseRepository,
    SlideVideoMappingRepository,
)


def _make_svm_mock(
    *, slide_number: int = 1, video_timecode: str = "00:05:00"
) -> MagicMock:
    """Create a mock SlideVideoMapping ORM object."""
    svm = MagicMock()
    svm.id = uuid.uuid4()
    svm.slide_number = slide_number
    svm.video_timecode = video_timecode
    return svm


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


class TestSlideVideoMappingAPI:
    @pytest.mark.asyncio
    async def test_create_slide_mapping_returns_201(self, client: AsyncClient) -> None:
        """POST /api/v1/courses/{id}/slide-mapping returns 201."""
        course_id = uuid.uuid4()
        records = [_make_svm_mock(slide_number=1), _make_svm_mock(slide_number=2)]
        with (
            patch.object(CourseRepository, "get_by_id", return_value=MagicMock()),
            patch.object(
                SlideVideoMappingRepository, "batch_create", return_value=records
            ),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/slide-mapping",
                json={
                    "mappings": [
                        {"slide_number": 1, "video_timecode": "00:05:00"},
                        {"slide_number": 2, "video_timecode": "00:15:00"},
                    ]
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["created"] == 2
        assert len(data["mappings"]) == 2

    @pytest.mark.asyncio
    async def test_create_slide_mapping_course_not_found(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping returns 404 for missing course."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            response = await client.post(
                f"/api/v1/courses/{uuid.uuid4()}/slide-mapping",
                json={
                    "mappings": [
                        {"slide_number": 1, "video_timecode": "00:05:00"},
                    ]
                },
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_slide_mapping_empty_list_returns_422(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping rejects empty mappings list."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/slide-mapping",
            json={"mappings": []},
        )
        assert response.status_code == 422


class TestSlideVideoMappingRepository:
    @pytest.mark.asyncio
    async def test_batch_create_adds_records(self, mock_session: AsyncMock) -> None:
        """batch_create() adds all mappings to session."""
        mappings = [
            SlideVideoMapEntry(slide_number=1, video_timecode="00:05:00"),
            SlideVideoMapEntry(slide_number=2, video_timecode="00:15:00"),
        ]
        repo = SlideVideoMappingRepository(mock_session)
        records = await repo.batch_create(uuid.uuid4(), mappings)
        assert len(records) == 2
        assert mock_session.add.call_count == 2
        mock_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_course_id_returns_list(self, mock_session: AsyncMock) -> None:
        """get_by_course_id() returns mappings ordered by slide_number."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            _make_svm_mock(slide_number=1),
            _make_svm_mock(slide_number=2),
        ]
        mock_session.execute.return_value = mock_result
        repo = SlideVideoMappingRepository(mock_session)
        results = await repo.get_by_course_id(uuid.uuid4())
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_by_course_id_returns_empty(
        self, mock_session: AsyncMock
    ) -> None:
        """get_by_course_id() returns empty list for no mappings."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        repo = SlideVideoMappingRepository(mock_session)
        results = await repo.get_by_course_id(uuid.uuid4())
        assert results == []
