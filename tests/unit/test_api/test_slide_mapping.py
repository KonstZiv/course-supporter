"""Tests for slide-video mapping API and SlideVideoMappingRepository."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import (
    CourseRepository,
    SlideVideoMappingRepository,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)


def _make_svm_mock(
    *,
    slide_number: int = 1,
    node_id: uuid.UUID | None = None,
    pres_id: uuid.UUID | None = None,
    vid_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock SlideVideoMapping ORM object."""
    svm = MagicMock()
    svm.id = uuid.uuid4()
    svm.node_id = node_id or uuid.uuid4()
    svm.presentation_entry_id = pres_id or uuid.uuid4()
    svm.video_entry_id = vid_id or uuid.uuid4()
    svm.slide_number = slide_number
    svm.video_timecode_start = "00:05:00"
    svm.video_timecode_end = "00:08:00"
    svm.order = 0
    svm.validation_state = "pending_validation"
    svm.blocking_factors = None
    svm.validation_errors = None
    svm.validated_at = None
    svm.created_at = datetime.now(UTC)
    return svm


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _make_mapping_payload(
    pres_id: uuid.UUID | None = None, vid_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Build a single mapping payload dict."""
    return {
        "presentation_entry_id": str(pres_id or uuid.uuid4()),
        "video_entry_id": str(vid_id or uuid.uuid4()),
        "slide_number": 3,
        "video_timecode_start": "00:05:30",
        "video_timecode_end": "00:08:15",
    }


class TestSlideVideoMappingAPI:
    @pytest.mark.asyncio
    async def test_create_slide_mapping_returns_201(self, client: AsyncClient) -> None:
        """POST /courses/{id}/nodes/{node_id}/slide-mapping returns 201."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_node = MagicMock()
        mock_node.course_id = course_id
        records = [
            _make_svm_mock(slide_number=1, node_id=node_id),
            _make_svm_mock(slide_number=2, node_id=node_id),
        ]
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(
                SlideVideoMappingRepository, "batch_create", return_value=records
            ),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload(), _make_mapping_payload()]},
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
                f"/api/v1/courses/{uuid.uuid4()}/nodes/{uuid.uuid4()}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_slide_mapping_node_not_found(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping returns 404 for missing node."""
        mock_course = MagicMock()
        mock_course.id = uuid.uuid4()
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            response = await client.post(
                f"/api/v1/courses/{mock_course.id}/nodes/{uuid.uuid4()}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_slide_mapping_node_wrong_course(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping returns 404 when node belongs to another course."""
        course_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_node = MagicMock()
        mock_node.course_id = uuid.uuid4()  # different course
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_slide_mapping_empty_list_returns_422(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping rejects empty mappings list."""
        response = await client.post(
            f"/api/v1/courses/{uuid.uuid4()}/nodes/{uuid.uuid4()}/slide-mapping",
            json={"mappings": []},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_response_includes_validation_state(
        self, client: AsyncClient
    ) -> None:
        """Response mappings include validation_state field."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_node = MagicMock()
        mock_node.course_id = course_id
        record = _make_svm_mock(node_id=node_id)
        record.validation_state = "validated"
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(
                SlideVideoMappingRepository, "batch_create", return_value=[record]
            ),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        mapping = response.json()["mappings"][0]
        assert mapping["validation_state"] == "validated"
        assert "video_timecode_start" in mapping
        assert "presentation_entry_id" in mapping


class TestSlideVideoMappingRepository:
    @pytest.mark.asyncio
    async def test_batch_create_adds_records(self, mock_session: AsyncMock) -> None:
        """batch_create() adds all mappings to session."""
        pres_id = uuid.uuid4()
        vid_id = uuid.uuid4()
        mappings = [
            SlideVideoMapEntry(
                presentation_entry_id=str(pres_id),
                video_entry_id=str(vid_id),
                slide_number=1,
                video_timecode_start="00:05:00",
            ),
            SlideVideoMapEntry(
                presentation_entry_id=str(pres_id),
                video_entry_id=str(vid_id),
                slide_number=2,
                video_timecode_start="00:15:00",
            ),
        ]
        repo = SlideVideoMappingRepository(mock_session)
        records = await repo.batch_create(uuid.uuid4(), mappings)
        assert len(records) == 2
        assert mock_session.add.call_count == 2
        mock_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batch_create_sets_order(self, mock_session: AsyncMock) -> None:
        """batch_create() assigns sequential order values."""
        pres_id = uuid.uuid4()
        vid_id = uuid.uuid4()
        mappings = [
            SlideVideoMapEntry(
                presentation_entry_id=str(pres_id),
                video_entry_id=str(vid_id),
                slide_number=i,
                video_timecode_start=f"00:0{i}:00",
            )
            for i in range(3)
        ]
        repo = SlideVideoMappingRepository(mock_session)
        records = await repo.batch_create(uuid.uuid4(), mappings)
        assert [r.order for r in records] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_get_by_node_id_returns_list(self, mock_session: AsyncMock) -> None:
        """get_by_node_id() returns mappings ordered by order."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            _make_svm_mock(slide_number=1),
            _make_svm_mock(slide_number=2),
        ]
        mock_session.execute.return_value = mock_result
        repo = SlideVideoMappingRepository(mock_session)
        results = await repo.get_by_node_id(uuid.uuid4())
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_by_node_id_returns_empty(self, mock_session: AsyncMock) -> None:
        """get_by_node_id() returns empty list for no mappings."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        repo = SlideVideoMappingRepository(mock_session)
        results = await repo.get_by_node_id(uuid.uuid4())
        assert results == []


class TestMappingValidationState:
    """Tests for the MappingValidationState enum."""

    def test_enum_values(self) -> None:
        from course_supporter.storage.orm import MappingValidationState

        assert MappingValidationState.VALIDATED == "validated"
        assert MappingValidationState.PENDING_VALIDATION == "pending_validation"
        assert MappingValidationState.VALIDATION_FAILED == "validation_failed"

    def test_enum_is_str(self) -> None:
        from course_supporter.storage.orm import MappingValidationState

        assert isinstance(MappingValidationState.VALIDATED, str)
