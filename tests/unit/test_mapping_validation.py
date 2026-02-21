"""Tests for MappingValidationService — structural validation (Level 1)."""

import uuid
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
    MappingValidationService,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

NODE_ID = uuid.uuid4()
PRES_ID = uuid.uuid4()
VID_ID = uuid.uuid4()
OTHER_NODE_ID = uuid.uuid4()


def _make_entry_mock(
    *,
    entry_id: uuid.UUID,
    node_id: uuid.UUID,
    source_type: str,
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.id = entry_id
    entry.node_id = node_id
    entry.source_type = source_type
    return entry


def _make_mapping(
    *,
    pres_id: uuid.UUID = PRES_ID,
    vid_id: uuid.UUID = VID_ID,
    tc_start: str = "01:23:45",
    tc_end: str | None = "01:30:00",
) -> SlideVideoMapEntry:
    return SlideVideoMapEntry(
        presentation_entry_id=str(pres_id),
        video_entry_id=str(vid_id),
        slide_number=1,
        video_timecode_start=tc_start,
        video_timecode_end=tc_end,
    )


def _session_with_entries(
    entries: dict[uuid.UUID, MagicMock],
) -> AsyncMock:
    """Build an AsyncSession mock that returns entries by ID."""
    session = AsyncMock()

    async def _execute(stmt: object) -> MagicMock:
        # Extract the UUID from the WHERE clause bind parameters
        compiled = stmt.compile()  # type: ignore[union-attr]
        params = compiled.params
        # The param key is auto-generated; grab first UUID value
        for val in params.values():
            if isinstance(val, uuid.UUID) and val in entries:
                result = MagicMock()
                result.scalar_one_or_none.return_value = entries[val]
                return result
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session.execute = _execute
    return session


class TestMappingValidationService:
    """Unit tests for validate_structural()."""

    @pytest.mark.asyncio
    async def test_valid_mapping_returns_no_errors(self) -> None:
        """Happy path — both entries exist, correct node and type."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert errors == []

    @pytest.mark.asyncio
    async def test_presentation_entry_not_found(self) -> None:
        """Missing presentation entry produces error."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "presentation_entry_id"
        assert "not found" in errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_node(self) -> None:
        """Presentation entry belonging to another node produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=OTHER_NODE_ID, source_type="presentation"
        )
        session = _session_with_entries({PRES_ID: pres})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "presentation_entry_id"
        assert "belongs to node" in errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_type(self) -> None:
        """Presentation entry with wrong source_type produces error."""
        pres = _make_entry_mock(entry_id=PRES_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "presentation_entry_id"
        assert "type 'video'" in errors[0].message

    @pytest.mark.asyncio
    async def test_video_entry_not_found(self) -> None:
        """Missing video entry produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        session = _session_with_entries({PRES_ID: pres})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "video_entry_id"
        assert "not found" in errors[0].message

    @pytest.mark.asyncio
    async def test_video_wrong_node(self) -> None:
        """Video entry belonging to another node produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID, node_id=OTHER_NODE_ID, source_type="video"
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "video_entry_id"
        assert "belongs to node" in errors[0].message

    @pytest.mark.asyncio
    async def test_video_wrong_type(self) -> None:
        """Video entry with wrong source_type produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="text")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].field == "video_entry_id"
        assert "type 'text'" in errors[0].message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_tc",
        ["abc", "1:2:3", "123:45", "1:2", ":12:34"],
    )
    async def test_invalid_timecode_format(self, bad_tc: str) -> None:
        """Invalid timecodes are rejected."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start=bad_tc, tc_end=None)
        errors = await svc.validate_structural(NODE_ID, mapping)
        assert len(errors) == 1
        assert errors[0].field == "video_timecode_start"
        assert "Invalid timecode format" in errors[0].message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tc",
        ["01:23:45", "23:45", "1:23:45", "00:00"],
    )
    async def test_valid_timecode_formats(self, tc: str) -> None:
        """Well-formed timecodes pass validation."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start=tc, tc_end=None)
        errors = await svc.validate_structural(NODE_ID, mapping)
        assert errors == []

    @pytest.mark.asyncio
    async def test_timecode_end_before_start(self) -> None:
        """timecode_end < timecode_start produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="01:00:00", tc_end="00:30:00")
        errors = await svc.validate_structural(NODE_ID, mapping)
        assert len(errors) == 1
        assert errors[0].field == "video_timecode_end"
        assert "before" in errors[0].message

    @pytest.mark.asyncio
    async def test_error_messages_contain_hints(self) -> None:
        """All validation errors include a hint field."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        errors = await svc.validate_structural(NODE_ID, _make_mapping())
        assert len(errors) == 1
        assert errors[0].hint is not None
        assert len(errors[0].hint) > 0


class TestRouteReturns422OnValidationError:
    """Integration test: route returns 422 when structural validation fails."""

    @pytest.fixture()
    def mock_session(self) -> AsyncMock:
        session = AsyncMock()
        session.add = MagicMock()
        return session

    @pytest.fixture()
    async def client(self, mock_session: AsyncMock) -> AsyncClient:
        app.dependency_overrides[get_session] = lambda: mock_session
        app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac  # type: ignore[misc]
        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_route_returns_422_on_validation_error(
        self, client: AsyncClient
    ) -> None:
        """POST slide-mapping returns 422 when validation fails."""
        from course_supporter.storage.material_node_repository import (
            MaterialNodeRepository,
        )
        from course_supporter.storage.repositories import CourseRepository

        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        mock_course = MagicMock()
        mock_course.id = course_id
        mock_node = MagicMock()
        mock_node.course_id = course_id

        validation_err = MappingValidationError(
            field="presentation_entry_id",
            message=f"Entry '{uuid.uuid4()}' not found",
            hint="Check that the entry ID is correct",
        )

        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(
                MappingValidationService,
                "validate_structural",
                return_value=[validation_err],
            ),
        ):
            response = await client.post(
                f"/api/v1/courses/{course_id}/nodes/{node_id}/slide-mapping",
                json={
                    "mappings": [
                        {
                            "presentation_entry_id": str(uuid.uuid4()),
                            "video_entry_id": str(uuid.uuid4()),
                            "slide_number": 1,
                            "video_timecode_start": "00:05:00",
                        }
                    ]
                },
            )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["index"] == 0
        assert detail[0]["errors"][0]["field"] == "presentation_entry_id"
        assert detail[0]["errors"][0]["hint"] is not None
