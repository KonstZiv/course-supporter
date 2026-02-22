"""Tests for MappingValidationService — structural (L1) + content (L2)."""

import json
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
    processed_content: str | None = None,
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.id = entry_id
    entry.node_id = node_id
    entry.source_type = source_type
    entry.processed_content = processed_content
    return entry


def _pres_content(page_count: int) -> str:
    """Build minimal presentation processed_content JSON."""
    return json.dumps({"metadata": {"page_count": page_count, "format": "pdf"}})


def _video_content(duration_sec: float) -> str:
    """Build minimal video processed_content JSON with one chunk."""
    return json.dumps(
        {
            "metadata": {"strategy": "whisper"},
            "chunks": [
                {"metadata": {"start_sec": 0.0, "end_sec": duration_sec}},
            ],
        }
    )


def _make_mapping(
    *,
    pres_id: uuid.UUID = PRES_ID,
    vid_id: uuid.UUID = VID_ID,
    slide_number: int = 1,
    tc_start: str = "01:23:45",
    tc_end: str | None = "01:30:00",
) -> SlideVideoMapEntry:
    return SlideVideoMapEntry(
        presentation_entry_id=str(pres_id),
        video_entry_id=str(vid_id),
        slide_number=slide_number,
        video_timecode_start=tc_start,
        video_timecode_end=tc_end,
    )


def _session_with_entries(
    entries: dict[uuid.UUID, MagicMock],
) -> AsyncMock:
    """Build an AsyncSession mock whose execute() returns pre-set entries.

    Handles both single-ID lookups and IN-clause queries by returning
    all matching entries from the ``entries`` dict.
    """
    session = AsyncMock()

    async def _execute(stmt: object) -> MagicMock:
        compiled = stmt.compile()  # type: ignore[union-attr]
        params = compiled.params
        matched = []
        for val in params.values():
            if isinstance(val, uuid.UUID) and val in entries:
                matched.append(entries[val])
            elif isinstance(val, (list, tuple, set)):
                for item in val:
                    if isinstance(item, uuid.UUID) and item in entries:
                        matched.append(entries[item])
        result = MagicMock()
        result.scalars.return_value.all.return_value = matched
        return result

    session.execute = _execute
    return session


class TestMappingValidationService:
    """Unit tests for validate_batch()."""

    @pytest.mark.asyncio
    async def test_valid_mapping_returns_no_errors(self) -> None:
        """Happy path — both entries exist, correct node and type."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert result == []

    @pytest.mark.asyncio
    async def test_presentation_entry_not_found(self) -> None:
        """Missing presentation entry produces error."""
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        _idx, errors = result[0]
        assert len(errors) == 1
        assert errors[0].field == "presentation_entry_id"
        assert "not found" in errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_node(self) -> None:
        """Presentation entry belonging to another node produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=OTHER_NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        _idx, errors = result[0]
        assert len(errors) == 1
        assert errors[0].field == "presentation_entry_id"
        assert "belongs to node" in errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_type(self) -> None:
        """Presentation entry with wrong source_type produces error."""
        pres = _make_entry_mock(entry_id=PRES_ID, node_id=NODE_ID, source_type="video")
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        _idx, errors = result[0]
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
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0][1][0].field == "video_entry_id"
        assert "not found" in result[0][1][0].message

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
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0][1][0].field == "video_entry_id"
        assert "belongs to node" in result[0][1][0].message

    @pytest.mark.asyncio
    async def test_video_wrong_type(self) -> None:
        """Video entry with wrong source_type produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="text")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0][1][0].field == "video_entry_id"
        assert "type 'text'" in result[0][1][0].message

    @pytest.mark.asyncio
    async def test_both_entry_errors_collected(self) -> None:
        """Both presentation and video errors returned in one pass."""
        # Neither entry exists — both should fail
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        _idx, errors = result[0]
        assert len(errors) == 2
        fields = {e.field for e in errors}
        assert fields == {"presentation_entry_id", "video_entry_id"}

    @pytest.mark.asyncio
    async def test_entry_and_timecode_errors_collected(self) -> None:
        """Entry error + timecode error returned together in one pass."""
        # Presentation missing + bad timecode
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="bad", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _idx, errors = result[0]
        assert len(errors) == 2
        fields = {e.field for e in errors}
        assert fields == {"presentation_entry_id", "video_timecode_start"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_tc",
        ["abc", "1:2:3", "123:45", "1:2", ":12:34", "00:99:99", "12:60:00"],
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
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0][1][0].field == "video_timecode_start"
        assert "Invalid timecode format" in result[0][1][0].message

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
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

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
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0][1][0].field == "video_timecode_end"
        assert "before" in result[0][1][0].message

    @pytest.mark.asyncio
    async def test_error_messages_contain_hints(self) -> None:
        """All validation errors include a hint field."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        for err in result[0][1]:
            assert err.hint is not None
            assert len(err.hint) > 0

    @pytest.mark.asyncio
    async def test_invalid_uuid_presentation(self) -> None:
        """Invalid UUID for presentation_entry_id returns 422, not 500."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        mapping = SlideVideoMapEntry(
            presentation_entry_id="not-a-uuid",
            video_entry_id=str(VID_ID),
            slide_number=1,
            video_timecode_start="00:05:00",
        )
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0][1][0].field == "presentation_entry_id"
        assert "Invalid UUID format" in result[0][1][0].message

    @pytest.mark.asyncio
    async def test_invalid_uuid_video(self) -> None:
        """Invalid UUID for video_entry_id returns validation error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        session = _session_with_entries({PRES_ID: pres})
        svc = MappingValidationService(session)
        mapping = SlideVideoMapEntry(
            presentation_entry_id=str(PRES_ID),
            video_entry_id="also-not-uuid",
            slide_number=1,
            video_timecode_start="00:05:00",
        )
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0][1][0].field == "video_entry_id"
        assert "Invalid UUID format" in result[0][1][0].message

    @pytest.mark.asyncio
    async def test_batch_single_query(self) -> None:
        """validate_batch() executes exactly one DB query for all mappings."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        original_execute = session.execute
        call_count = 0

        async def counting_execute(stmt: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return await original_execute(stmt)

        session.execute = counting_execute

        svc = MappingValidationService(session)
        mappings = [_make_mapping() for _ in range(5)]
        result = await svc.validate_batch(NODE_ID, mappings)
        assert result == []
        assert call_count == 1, f"Expected 1 DB query, got {call_count}"

    @pytest.mark.asyncio
    async def test_batch_multiple_errors(self) -> None:
        """validate_batch() returns errors for all failing mappings."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        mappings = [_make_mapping(), _make_mapping()]
        result = await svc.validate_batch(NODE_ID, mappings)
        assert len(result) == 2
        assert result[0][0] == 0
        assert result[1][0] == 1


class TestContentValidationLevel2:
    """Unit tests for Level 2 content validation."""

    @pytest.mark.asyncio
    async def test_slide_number_out_of_range_high(self) -> None:
        """Slide number exceeding page_count produces error with range hint."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(30),
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=42, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        err = result[0][1][0]
        assert err.field == "slide_number"
        assert "42" in err.message
        assert "30" in err.message
        assert "1\u201330" in err.hint  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_slide_number_zero(self) -> None:
        """Slide number 0 is invalid (range starts at 1)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=0, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0][1][0].field == "slide_number"

    @pytest.mark.asyncio
    async def test_slide_number_boundary_first(self) -> None:
        """Slide number 1 is valid (first slide)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=1, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_slide_number_boundary_last(self) -> None:
        """Slide number equal to page_count is valid (last slide)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="video")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_timecode_exceeds_video_duration(self) -> None:
        """Timecode beyond video duration produces error with range hint."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(600.0),  # 10:00
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        # 15:00 = 900s > 600s
        mapping = _make_mapping(tc_start="15:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        err = result[0][1][0]
        assert err.field == "video_timecode_start"
        assert "900s" in err.message
        assert "10:00" in err.message
        assert "10:00" in err.hint  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_timecode_end_exceeds_video_duration(self) -> None:
        """timecode_end beyond video duration produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(300.0),  # 05:00
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="04:00", tc_end="06:00")
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        err = result[0][1][0]
        assert err.field == "video_timecode_end"
        assert "exceeds" in err.message

    @pytest.mark.asyncio
    async def test_timecode_at_exact_duration_is_valid(self) -> None:
        """Timecode equal to video duration is valid (boundary)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),  # 1:30:00
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="1:30:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_processed_content_skips_level2(self) -> None:
        """When processed_content is None, Level 2 checks are skipped."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=None,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=None,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        # slide=999 would fail L2, but is skipped
        mapping = _make_mapping(slide_number=999, tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_pres_ready_video_not_ready(self) -> None:
        """L2 validates slide_number but skips timecode when video not ready."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(5),
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=None,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        # Only slide_number error, no timecode error
        assert len(result) == 1
        assert len(result[0][1]) == 1
        assert result[0][1][0].field == "slide_number"

    @pytest.mark.asyncio
    async def test_video_empty_chunks_skips_timecode_check(self) -> None:
        """Video with no chunks (no duration) skips timecode range check."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=json.dumps({"metadata": {}, "chunks": []}),
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_malformed_processed_content_skips_level2(self) -> None:
        """Malformed JSON in processed_content gracefully skips L2."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content="not json",
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content="{broken",
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=999, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []

    @pytest.mark.asyncio
    async def test_slide_and_timecode_errors_collected_together(self) -> None:
        """L2 collects both slide_number and timecode errors in one pass."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(5),
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(60.0),
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_start="05:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        errors = result[0][1]
        assert len(errors) == 2
        fields = {e.field for e in errors}
        assert fields == {"slide_number", "video_timecode_start"}

    @pytest.mark.asyncio
    async def test_valid_content_no_errors(self) -> None:
        """Fully valid mapping with content checks passes all levels."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(30),
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=15, tc_start="01:00:00", tc_end="01:15:00")
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert result == []


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
                "validate_batch",
                return_value=[(0, [validation_err])],
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
