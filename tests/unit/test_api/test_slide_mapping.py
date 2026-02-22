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
from course_supporter.storage.mapping_validation import (
    MappingValidationError,
    MappingValidationResult,
    MappingValidationService,
)
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.orm import MappingValidationState
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
    pres_id: uuid.UUID | None = None,
    vid_id: uuid.UUID | None = None,
    slide_number: int = 3,
    tc_start: str = "00:05:30",
) -> dict[str, object]:
    """Build a single mapping payload dict."""
    return {
        "presentation_entry_id": str(pres_id or uuid.uuid4()),
        "video_entry_id": str(vid_id or uuid.uuid4()),
        "slide_number": slide_number,
        "video_timecode_start": tc_start,
        "video_timecode_end": "00:08:15",
    }


def _validated_result(index: int) -> MappingValidationResult:
    """Build a VALIDATED result for a given index."""
    return MappingValidationResult(
        index=index,
        status=MappingValidationState.VALIDATED,
        errors=[],
        blocking_factors=[],
    )


def _failed_result(index: int) -> MappingValidationResult:
    """Build a VALIDATION_FAILED result for a given index."""
    return MappingValidationResult(
        index=index,
        status=MappingValidationState.VALIDATION_FAILED,
        errors=[
            MappingValidationError(
                field="presentation_entry_id",
                message="Entry not found",
                hint="Check that the entry ID is correct",
            ),
        ],
        blocking_factors=[],
    )


def _route_patches(
    course_id: uuid.UUID,
    node_id: uuid.UUID,
    validation_results: list[MappingValidationResult],
    batch_create_records: list[MagicMock],
    existing_mappings: list[MagicMock] | None = None,
):
    """Context manager with all common patches for the slide-mapping route."""
    mock_course = MagicMock()
    mock_course.id = course_id
    mock_node = MagicMock()
    mock_node.course_id = course_id

    return (
        patch.object(CourseRepository, "get_by_id", return_value=mock_course),
        patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
        patch.object(
            MappingValidationService,
            "validate_batch",
            return_value=validation_results,
        ),
        patch.object(
            SlideVideoMappingRepository,
            "get_by_node_id",
            return_value=existing_mappings or [],
        ),
        patch.object(
            SlideVideoMappingRepository,
            "batch_create",
            return_value=batch_create_records,
        ),
    )


class TestSlideVideoMappingAPI:
    @pytest.mark.asyncio
    async def test_full_success_returns_201(self, client: AsyncClient) -> None:
        """All mappings valid → 201, failed=0, skipped=0."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        records = [
            _make_svm_mock(slide_number=1, node_id=node_id),
            _make_svm_mock(slide_number=2, node_id=node_id),
        ]
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0), _validated_result(1)],
            records,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload(), _make_mapping_payload()]},
            )
        assert response.status_code == 201
        data = response.json()
        assert data["created"] == 2
        assert data["skipped"] == 0
        assert data["failed"] == 0
        assert len(data["mappings"]) == 2
        assert data["rejected"] == []
        assert data["skipped_items"] == []
        assert data["hints"] == {}

    @pytest.mark.asyncio
    async def test_partial_success_returns_207(self, client: AsyncClient) -> None:
        """Some created, some rejected → 207."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        records = [_make_svm_mock(slide_number=1, node_id=node_id)]
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0), _failed_result(1)],
            records,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload(), _make_mapping_payload()]},
            )
        assert response.status_code == 207
        data = response.json()
        assert data["created"] == 1
        assert data["failed"] == 1
        assert len(data["rejected"]) == 1
        assert data["rejected"][0]["index"] == 1

    @pytest.mark.asyncio
    async def test_all_failed_returns_422(self, client: AsyncClient) -> None:
        """All invalid → 422."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        patches = _route_patches(
            course_id,
            node_id,
            [_failed_result(0), _failed_result(1)],
            [],  # no records created
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload(), _make_mapping_payload()]},
            )
        assert response.status_code == 422
        data = response.json()
        assert data["created"] == 0
        assert data["failed"] == 2

    @pytest.mark.asyncio
    async def test_duplicate_skipped_returns_201(self, client: AsyncClient) -> None:
        """Duplicate submit → skipped=N, 201."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        pres_id = uuid.uuid4()
        vid_id = uuid.uuid4()
        existing = _make_svm_mock(node_id=node_id, pres_id=pres_id, vid_id=vid_id)
        existing.slide_number = 3
        existing.video_timecode_start = "00:05:30"

        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0)],
            [],  # nothing created
            existing_mappings=[existing],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={
                    "mappings": [_make_mapping_payload(pres_id=pres_id, vid_id=vid_id)]
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["created"] == 0
        assert data["skipped"] == 1
        assert data["failed"] == 0
        assert data["skipped_items"][0]["hint"] == "already exists"

    @pytest.mark.asyncio
    async def test_mixed_duplicate_and_new(self, client: AsyncClient) -> None:
        """Mix of new and duplicate → correct counts."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        pres_id = uuid.uuid4()
        vid_id = uuid.uuid4()
        existing = _make_svm_mock(node_id=node_id, pres_id=pres_id, vid_id=vid_id)
        existing.slide_number = 3
        existing.video_timecode_start = "00:05:30"

        new_record = _make_svm_mock(slide_number=4, node_id=node_id)
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0), _validated_result(1)],
            [new_record],
            existing_mappings=[existing],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={
                    "mappings": [
                        _make_mapping_payload(pres_id=pres_id, vid_id=vid_id),
                        _make_mapping_payload(slide_number=4, tc_start="00:10:00"),
                    ]
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["created"] == 1
        assert data["skipped"] == 1
        assert data["failed"] == 0

    @pytest.mark.asyncio
    async def test_partial_with_skipped_and_rejected(self, client: AsyncClient) -> None:
        """Created + skipped + rejected → 207."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        pres_id = uuid.uuid4()
        vid_id = uuid.uuid4()
        existing = _make_svm_mock(node_id=node_id, pres_id=pres_id, vid_id=vid_id)
        existing.slide_number = 3
        existing.video_timecode_start = "00:05:30"

        new_record = _make_svm_mock(slide_number=4, node_id=node_id)
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0), _validated_result(1), _failed_result(2)],
            [new_record],
            existing_mappings=[existing],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={
                    "mappings": [
                        _make_mapping_payload(pres_id=pres_id, vid_id=vid_id),
                        _make_mapping_payload(slide_number=4, tc_start="00:10:00"),
                        _make_mapping_payload(),
                    ]
                },
            )
        assert response.status_code == 207
        data = response.json()
        assert data["created"] == 1
        assert data["skipped"] == 1
        assert data["failed"] == 1

    @pytest.mark.asyncio
    async def test_response_includes_hints_on_failure(
        self, client: AsyncClient
    ) -> None:
        """Hints present when rejected > 0."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        records = [_make_svm_mock(node_id=node_id)]
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0), _failed_result(1)],
            records,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload(), _make_mapping_payload()]},
            )
        data = response.json()
        assert "resubmit" in data["hints"]
        assert "batch_size" in data["hints"]

    @pytest.mark.asyncio
    async def test_response_no_hints_on_full_success(self, client: AsyncClient) -> None:
        """Hints empty when full success."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        records = [_make_svm_mock(node_id=node_id)]
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0)],
            records,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        data = response.json()
        assert data["hints"] == {}

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
        record = _make_svm_mock(node_id=node_id)
        record.validation_state = "validated"
        patches = _route_patches(
            course_id,
            node_id,
            [_validated_result(0)],
            [record],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={"mappings": [_make_mapping_payload()]},
            )
        mapping = response.json()["mappings"][0]
        assert mapping["validation_state"] == "validated"
        assert "video_timecode_start" in mapping
        assert "presentation_entry_id" in mapping


class TestListSlideMapping:
    """Tests for GET /courses/{id}/nodes/{node_id}/slide-mapping."""

    @pytest.mark.asyncio
    async def test_list_returns_200_with_mappings(self, client: AsyncClient) -> None:
        """Happy path — returns mappings list."""
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
                SlideVideoMappingRepository, "get_by_node_id", return_value=records
            ),
        ):
            response = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_returns_empty_list(self, client: AsyncClient) -> None:
        """Node with no mappings → 200, items=[], total=0."""
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_node = MagicMock()
        mock_node.course_id = course_id
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(
                SlideVideoMappingRepository, "get_by_node_id", return_value=[]
            ),
        ):
            response = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_list_course_not_found_returns_404(self, client: AsyncClient) -> None:
        """GET slide-mapping returns 404 for missing course."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            response = await client.get(
                f"/api/v1/courses/{uuid.uuid4()}/nodes/{uuid.uuid4()}/slide-mapping",
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Course not found"

    @pytest.mark.asyncio
    async def test_list_node_not_found_returns_404(self, client: AsyncClient) -> None:
        """GET slide-mapping returns 404 for missing node."""
        course_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=None),
        ):
            response = await client.get(
                f"/api/v1/courses/{course_id}/nodes/{uuid.uuid4()}/slide-mapping",
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Node not found"


class TestDeleteSlideMapping:
    """Tests for DELETE /courses/{id}/slide-mapping/{mapping_id}."""

    @pytest.mark.asyncio
    async def test_delete_returns_204(self, client: AsyncClient) -> None:
        """Happy path — mapping deleted."""
        course_id = uuid.uuid4()
        mapping_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_mapping = MagicMock()
        mock_mapping.node_id = node_id
        mock_node = MagicMock()
        mock_node.course_id = course_id
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(
                SlideVideoMappingRepository, "get_by_id", return_value=mock_mapping
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(SlideVideoMappingRepository, "delete", return_value=None),
        ):
            response = await client.delete(
                f"/api/v1/courses/{course_id}/slide-mapping/{mapping_id}",
            )
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_course_not_found_returns_404(
        self, client: AsyncClient
    ) -> None:
        """DELETE returns 404 for missing course."""
        with patch.object(CourseRepository, "get_by_id", return_value=None):
            response = await client.delete(
                f"/api/v1/courses/{uuid.uuid4()}/slide-mapping/{uuid.uuid4()}",
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Course not found"

    @pytest.mark.asyncio
    async def test_delete_mapping_not_found_returns_404(
        self, client: AsyncClient
    ) -> None:
        """DELETE returns 404 for missing mapping."""
        course_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(SlideVideoMappingRepository, "get_by_id", return_value=None),
        ):
            response = await client.delete(
                f"/api/v1/courses/{course_id}/slide-mapping/{uuid.uuid4()}",
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Mapping not found"

    @pytest.mark.asyncio
    async def test_delete_mapping_wrong_course_returns_404(
        self, client: AsyncClient
    ) -> None:
        """DELETE returns 404 when mapping belongs to node in another course."""
        course_id = uuid.uuid4()
        mapping_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_mapping = MagicMock()
        mock_mapping.node_id = node_id
        mock_node = MagicMock()
        mock_node.course_id = uuid.uuid4()  # different course
        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(
                SlideVideoMappingRepository, "get_by_id", return_value=mock_mapping
            ),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
        ):
            response = await client.delete(
                f"/api/v1/courses/{course_id}/slide-mapping/{mapping_id}",
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Mapping not found in this course"


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
    async def test_get_by_id_returns_mapping(self, mock_session: AsyncMock) -> None:
        """get_by_id() returns mapping when found."""
        mapping_id = uuid.uuid4()
        mock_mapping = _make_svm_mock()
        mock_session.get.return_value = mock_mapping
        repo = SlideVideoMappingRepository(mock_session)
        result = await repo.get_by_id(mapping_id)
        assert result is mock_mapping
        mock_session.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none(self, mock_session: AsyncMock) -> None:
        """get_by_id() returns None when not found."""
        mock_session.get.return_value = None
        repo = SlideVideoMappingRepository(mock_session)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_removes_mapping(self, mock_session: AsyncMock) -> None:
        """delete() removes mapping and flushes."""
        mock_mapping = _make_svm_mock()
        mock_session.get.return_value = mock_mapping
        repo = SlideVideoMappingRepository(mock_session)
        await repo.delete(mock_mapping.id)
        mock_session.delete.assert_awaited_once_with(mock_mapping)
        mock_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_not_found_raises(self, mock_session: AsyncMock) -> None:
        """delete() raises ValueError when mapping not found."""
        mock_session.get.return_value = None
        repo = SlideVideoMappingRepository(mock_session)
        with pytest.raises(ValueError, match="SlideVideoMapping not found"):
            await repo.delete(uuid.uuid4())

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
        assert MappingValidationState.VALIDATED == "validated"
        assert MappingValidationState.PENDING_VALIDATION == "pending_validation"
        assert MappingValidationState.VALIDATION_FAILED == "validation_failed"

    def test_enum_is_str(self) -> None:
        assert isinstance(MappingValidationState.VALIDATED, str)
