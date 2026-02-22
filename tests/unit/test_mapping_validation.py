"""Tests for MappingValidationService — structural (L1), content (L2), deferred (L3),
and auto-revalidation (S2-042).
"""

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
    MappingValidationResult,
    MappingValidationService,
)
from course_supporter.storage.orm import MappingValidationState, MaterialState

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
    state: MaterialState = MaterialState.RAW,
) -> MagicMock:
    """Create a mock MaterialEntry."""
    entry = MagicMock()
    entry.id = entry_id
    entry.node_id = node_id
    entry.source_type = source_type
    entry.processed_content = processed_content
    entry.state = state
    entry.filename = None
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


def _assert_all_validated(results: list[MappingValidationResult]) -> None:
    """Assert all results are VALIDATED with no errors or blockers."""
    for r in results:
        assert r.status == MappingValidationState.VALIDATED
        assert r.errors == []
        assert r.blocking_factors == []


class TestMappingValidationService:
    """Unit tests for validate_batch()."""

    @pytest.mark.asyncio
    async def test_valid_mapping_returns_no_errors(self) -> None:
        """Happy path — both entries exist, correct node and type."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_presentation_entry_not_found(self) -> None:
        """Missing presentation entry produces error."""
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 1
        assert result[0].errors[0].field == "presentation_entry_id"
        assert "not found" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_node(self) -> None:
        """Presentation entry belonging to another node produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID, node_id=OTHER_NODE_ID, source_type="presentation"
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 1
        assert result[0].errors[0].field == "presentation_entry_id"
        assert "belongs to node" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_presentation_wrong_type(self) -> None:
        """Presentation entry with wrong source_type produces error."""
        pres = _make_entry_mock(entry_id=PRES_ID, node_id=NODE_ID, source_type="video")
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 1
        assert result[0].errors[0].field == "presentation_entry_id"
        assert "type 'video'" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_video_entry_not_found(self) -> None:
        """Missing video entry produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].errors[0].field == "video_entry_id"
        assert "not found" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_video_wrong_node(self) -> None:
        """Video entry belonging to another node produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID, node_id=OTHER_NODE_ID, source_type="video"
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].errors[0].field == "video_entry_id"
        assert "belongs to node" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_video_wrong_type(self) -> None:
        """Video entry with wrong source_type produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(entry_id=VID_ID, node_id=NODE_ID, source_type="text")
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].errors[0].field == "video_entry_id"
        assert "type 'text'" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_both_entry_errors_collected(self) -> None:
        """Both presentation and video errors returned in one pass."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 2
        fields = {e.field for e in result[0].errors}
        assert fields == {"presentation_entry_id", "video_entry_id"}

    @pytest.mark.asyncio
    async def test_entry_and_timecode_errors_collected(self) -> None:
        """Entry error + timecode error returned together in one pass."""
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="bad", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 2
        fields = {e.field for e in result[0].errors}
        assert fields == {"presentation_entry_id", "video_timecode_start"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_tc",
        ["abc", "1:2:3", "123:45", "1:2", ":12:34", "00:99:99", "12:60:00"],
    )
    async def test_invalid_timecode_format(self, bad_tc: str) -> None:
        """Invalid timecodes are rejected."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start=bad_tc, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[0].errors[0].field == "video_timecode_start"
        assert "Invalid timecode format" in result[0].errors[0].message

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tc",
        ["01:23:45", "23:45", "1:23:45", "00:00"],
    )
    async def test_valid_timecode_formats(self, tc: str) -> None:
        """Well-formed timecodes pass validation."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start=tc, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_timecode_end_before_start(self) -> None:
        """timecode_end < timecode_start produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="01:00:00", tc_end="00:30:00")
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[0].errors[0].field == "video_timecode_end"
        assert "before" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_error_messages_contain_hints(self) -> None:
        """All validation errors include a hint field."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        for err in result[0].errors:
            assert err.hint is not None
            assert len(err.hint) > 0

    @pytest.mark.asyncio
    async def test_invalid_uuid_presentation(self) -> None:
        """Invalid UUID for presentation_entry_id returns validation error."""
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
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[0].errors[0].field == "presentation_entry_id"
        assert "Invalid UUID format" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_invalid_uuid_video(self) -> None:
        """Invalid UUID for video_entry_id returns validation error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
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
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[0].errors[0].field == "video_entry_id"
        assert "Invalid UUID format" in result[0].errors[0].message

    @pytest.mark.asyncio
    async def test_batch_single_query(self) -> None:
        """validate_batch() executes exactly one DB query for all mappings."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
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
        assert len(result) == 5
        _assert_all_validated(result)
        assert call_count == 1, f"Expected 1 DB query, got {call_count}"

    @pytest.mark.asyncio
    async def test_batch_multiple_errors(self) -> None:
        """validate_batch() returns results for all mappings."""
        session = _session_with_entries({})
        svc = MappingValidationService(session)
        mappings = [_make_mapping(), _make_mapping()]
        result = await svc.validate_batch(NODE_ID, mappings)
        assert len(result) == 2
        assert result[0].index == 0
        assert result[1].index == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[1].status == MappingValidationState.VALIDATION_FAILED


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
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=42, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        err = result[0].errors[0]
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
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=0, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert result[0].errors[0].field == "slide_number"

    @pytest.mark.asyncio
    async def test_slide_number_boundary_first(self) -> None:
        """Slide number 1 is valid (first slide)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=1, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_slide_number_boundary_last(self) -> None:
        """Slide number equal to page_count is valid (last slide)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_timecode_exceeds_video_duration(self) -> None:
        """Timecode beyond video duration produces error with range hint."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(600.0),  # 10:00
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        # 15:00 = 900s > 600s
        mapping = _make_mapping(tc_start="15:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        err = result[0].errors[0]
        assert err.field == "video_timecode_start"
        assert "900s" in err.message
        assert "10:00" in err.message
        assert "10:00" in err.hint  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_timecode_end_exceeds_video_duration(self) -> None:
        """timecode_end beyond video duration produces error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(300.0),  # 05:00
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="04:00", tc_end="06:00")
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        err = result[0].errors[0]
        assert err.field == "video_timecode_end"
        assert "exceeds" in err.message

    @pytest.mark.asyncio
    async def test_timecode_at_exact_duration_is_valid(self) -> None:
        """Timecode equal to video duration is valid (boundary)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),  # 1:30:00
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="1:30:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_no_processed_content_raw_produces_blocker(self) -> None:
        """When entries are RAW, Level 2 is skipped and blockers are created."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=None,
            state=MaterialState.RAW,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=None,
            state=MaterialState.RAW,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        # slide=999 would fail L2, but is skipped
        mapping = _make_mapping(slide_number=999, tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION
        assert len(result[0].blocking_factors) == 2
        assert result[0].errors == []

    @pytest.mark.asyncio
    async def test_pres_ready_video_not_ready(self) -> None:
        """L2 validates slide_number but skips timecode when video not ready."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(5),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=None,
            state=MaterialState.RAW,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        # slide_number error from L2 + video blocker → VALIDATION_FAILED (error wins)
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) == 1
        assert result[0].errors[0].field == "slide_number"
        assert len(result[0].blocking_factors) == 1

    @pytest.mark.asyncio
    async def test_video_empty_chunks_skips_timecode_check(self) -> None:
        """Video with no chunks (no duration) skips timecode range check."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=json.dumps({"metadata": {}, "chunks": []}),
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(tc_start="99:59:59", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_malformed_processed_content_skips_level2(self) -> None:
        """Malformed JSON in processed_content gracefully skips L2."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content="not json",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content="{broken",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=999, tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)

    @pytest.mark.asyncio
    async def test_slide_and_timecode_errors_collected_together(self) -> None:
        """L2 collects both slide_number and timecode errors in one pass."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(5),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(60.0),
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=10, tc_start="05:00", tc_end=None)
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        errors = result[0].errors
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
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        mapping = _make_mapping(slide_number=15, tc_start="01:00:00", tc_end="01:15:00")
        result = await svc.validate_batch(NODE_ID, [mapping])
        assert len(result) == 1
        _assert_all_validated(result)


class TestDeferredValidationLevel3:
    """Unit tests for Level 3 deferred validation (blocking factors)."""

    @pytest.mark.asyncio
    async def test_pending_material_produces_blocking_factor(self) -> None:
        """Entry state=PENDING produces PENDING_VALIDATION with blocker."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.PENDING,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.PENDING,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION
        assert result[0].errors == []
        assert len(result[0].blocking_factors) == 2

    @pytest.mark.asyncio
    async def test_raw_material_produces_blocking_factor(self) -> None:
        """Entry state=RAW produces PENDING_VALIDATION with blocker."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.RAW,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.RAW,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION

    @pytest.mark.asyncio
    async def test_error_material_produces_material_error_blocker(self) -> None:
        """Entry state=ERROR produces blocker with type=material_error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.ERROR,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION
        assert len(result[0].blocking_factors) == 1
        assert result[0].blocking_factors[0].type == "material_error"

    @pytest.mark.asyncio
    async def test_integrity_broken_produces_material_error_blocker(self) -> None:
        """Entry state=INTEGRITY_BROKEN produces blocker with type=material_error."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.INTEGRITY_BROKEN,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION
        assert result[0].blocking_factors[0].type == "material_error"

    @pytest.mark.asyncio
    async def test_blocking_factor_fields_populated(self) -> None:
        """All blocking factor fields are correctly populated."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.PENDING,
        )
        pres.filename = "slides.pdf"
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        bf = result[0].blocking_factors[0]
        assert bf.material_entry_id == str(PRES_ID)
        assert bf.filename == "slides.pdf"
        assert bf.material_state == "pending"
        assert "pending" in bf.message
        assert bf.type == "material_not_ready"

    @pytest.mark.asyncio
    async def test_pres_blocker_blocked_checks(self) -> None:
        """Presentation blocker has blocked_checks=["slide_number"]."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.RAW,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        pres_blocker = [
            bf
            for bf in result[0].blocking_factors
            if bf.material_entry_id == str(PRES_ID)
        ]
        assert len(pres_blocker) == 1
        assert pres_blocker[0].blocked_checks == ["slide_number"]

    @pytest.mark.asyncio
    async def test_video_blocker_blocked_checks(self) -> None:
        """Video blocker has blocked_checks with timecode fields."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.RAW,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        vid_blocker = [
            bf
            for bf in result[0].blocking_factors
            if bf.material_entry_id == str(VID_ID)
        ]
        assert len(vid_blocker) == 1
        assert vid_blocker[0].blocked_checks == [
            "video_timecode_start",
            "video_timecode_end",
        ]

    @pytest.mark.asyncio
    async def test_both_entries_not_ready_two_blockers(self) -> None:
        """Both entries non-READY produces two blocking factors."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.PENDING,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.ERROR,
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        assert result[0].status == MappingValidationState.PENDING_VALIDATION
        assert len(result[0].blocking_factors) == 2
        types = {bf.type for bf in result[0].blocking_factors}
        assert types == {"material_not_ready", "material_error"}

    @pytest.mark.asyncio
    async def test_mixed_batch_validated_and_pending(self) -> None:
        """Batch with one READY pair and one non-READY pair."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        pres2_id = uuid.uuid4()
        vid2_id = uuid.uuid4()
        pres2 = _make_entry_mock(
            entry_id=pres2_id,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.PENDING,
        )
        vid2 = _make_entry_mock(
            entry_id=vid2_id,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.PENDING,
        )
        session = _session_with_entries(
            {PRES_ID: pres, VID_ID: vid, pres2_id: pres2, vid2_id: vid2}
        )
        svc = MappingValidationService(session)
        mappings = [
            _make_mapping(),
            _make_mapping(pres_id=pres2_id, vid_id=vid2_id),
        ]
        result = await svc.validate_batch(NODE_ID, mappings)
        assert len(result) == 2
        assert result[0].status == MappingValidationState.VALIDATED
        assert result[1].status == MappingValidationState.PENDING_VALIDATION

    @pytest.mark.asyncio
    async def test_l1_error_overrides_blocker(self) -> None:
        """L1 failure takes precedence: VALIDATION_FAILED, not PENDING."""
        # presentation not found (L1 error) + video not ready (would be blocker)
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.RAW,
        )
        session = _session_with_entries({VID_ID: vid})
        svc = MappingValidationService(session)
        result = await svc.validate_batch(NODE_ID, [_make_mapping()])
        assert len(result) == 1
        # L1 error → VALIDATION_FAILED despite video having a blocker
        assert result[0].status == MappingValidationState.VALIDATION_FAILED
        assert len(result[0].errors) >= 1
        assert result[0].errors[0].field == "presentation_entry_id"


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
        failed_result = MappingValidationResult(
            index=0,
            status=MappingValidationState.VALIDATION_FAILED,
            errors=[validation_err],
            blocking_factors=[],
        )

        with (
            patch.object(CourseRepository, "get_by_id", return_value=mock_course),
            patch.object(MaterialNodeRepository, "get_by_id", return_value=mock_node),
            patch.object(
                MappingValidationService,
                "validate_batch",
                return_value=[failed_result],
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


def _make_orm_mapping(
    *,
    node_id: uuid.UUID = NODE_ID,
    pres_id: uuid.UUID = PRES_ID,
    vid_id: uuid.UUID = VID_ID,
    slide_number: int = 1,
    tc_start: str = "01:23:45",
    tc_end: str | None = "01:30:00",
    blocking_factors: list[dict[str, object]] | None = None,
) -> MagicMock:
    """Create a mock SlideVideoMapping ORM record."""
    m = MagicMock()
    m.id = uuid.uuid4()
    m.node_id = node_id
    m.presentation_entry_id = pres_id
    m.video_entry_id = vid_id
    m.slide_number = slide_number
    m.video_timecode_start = tc_start
    m.video_timecode_end = tc_end
    m.blocking_factors = blocking_factors
    m.validation_state = MappingValidationState.PENDING_VALIDATION
    m.validation_errors = None
    m.validated_at = None
    return m


class TestAutoRevalidation:
    """Tests for revalidate_blocked() — S2-042."""

    @pytest.mark.asyncio
    async def test_revalidate_material_becomes_ready(self) -> None:
        """Both materials READY → mapping becomes VALIDATED."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(10),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),
            state=MaterialState.READY,
        )
        orm_mapping = _make_orm_mapping(
            blocking_factors=[
                {"material_entry_id": str(VID_ID), "type": "material_not_ready"}
            ],
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=[orm_mapping]
            )
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(VID_ID)

        assert count == 1
        assert orm_mapping.validation_state == MappingValidationState.VALIDATED
        assert orm_mapping.validation_errors is None
        assert orm_mapping.blocking_factors is None

    @pytest.mark.asyncio
    async def test_revalidate_material_becomes_error(self) -> None:
        """One material ERROR → blocker updated, PENDING_VALIDATION."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.ERROR,
        )
        orm_mapping = _make_orm_mapping(
            blocking_factors=[
                {"material_entry_id": str(VID_ID), "type": "material_not_ready"}
            ],
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=[orm_mapping]
            )
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(VID_ID)

        assert count == 1
        assert orm_mapping.validation_state == MappingValidationState.PENDING_VALIDATION
        assert orm_mapping.blocking_factors is not None
        assert orm_mapping.blocking_factors[0]["type"] == "material_error"

    @pytest.mark.asyncio
    async def test_revalidate_one_ready_one_still_pending(self) -> None:
        """One READY, other PENDING → PENDING_VALIDATION (one blocker removed)."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.PENDING,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),
            state=MaterialState.READY,
        )
        orm_mapping = _make_orm_mapping(
            blocking_factors=[
                {"material_entry_id": str(PRES_ID), "type": "material_not_ready"},
                {"material_entry_id": str(VID_ID), "type": "material_not_ready"},
            ],
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=[orm_mapping]
            )
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(VID_ID)

        assert count == 1
        assert orm_mapping.validation_state == MappingValidationState.PENDING_VALIDATION
        # Only pres blocker remains
        assert len(orm_mapping.blocking_factors) == 1
        assert orm_mapping.blocking_factors[0]["material_entry_id"] == str(PRES_ID)

    @pytest.mark.asyncio
    async def test_revalidate_ready_but_l2_fails(self) -> None:
        """READY but slide out of range → VALIDATION_FAILED."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            processed_content=_pres_content(5),
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            processed_content=_video_content(5400.0),
            state=MaterialState.READY,
        )
        orm_mapping = _make_orm_mapping(
            slide_number=42,
            blocking_factors=[
                {"material_entry_id": str(PRES_ID), "type": "material_not_ready"}
            ],
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=[orm_mapping]
            )
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(PRES_ID)

        assert count == 1
        assert orm_mapping.validation_state == MappingValidationState.VALIDATION_FAILED
        assert orm_mapping.validation_errors is not None
        assert orm_mapping.validation_errors[0]["field"] == "slide_number"

    @pytest.mark.asyncio
    async def test_revalidate_no_pending_mappings_noop(self) -> None:
        """No blocked mappings → count=0, no flush."""
        session = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(return_value=[])
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(uuid.uuid4())

        assert count == 0
        session.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revalidate_returns_count(self) -> None:
        """Returns correct count of revalidated mappings."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        mappings = [
            _make_orm_mapping(
                blocking_factors=[
                    {"material_entry_id": str(PRES_ID), "type": "material_not_ready"}
                ],
            )
            for _ in range(3)
        ]
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=mappings
            )
            svc = MappingValidationService(session)
            count = await svc.revalidate_blocked(PRES_ID)

        assert count == 3

    @pytest.mark.asyncio
    async def test_revalidate_sets_validated_at(self) -> None:
        """VALIDATED mapping gets validated_at set."""
        pres = _make_entry_mock(
            entry_id=PRES_ID,
            node_id=NODE_ID,
            source_type="presentation",
            state=MaterialState.READY,
        )
        vid = _make_entry_mock(
            entry_id=VID_ID,
            node_id=NODE_ID,
            source_type="video",
            state=MaterialState.READY,
        )
        orm_mapping = _make_orm_mapping(
            blocking_factors=[
                {"material_entry_id": str(VID_ID), "type": "material_not_ready"}
            ],
        )
        session = _session_with_entries({PRES_ID: pres, VID_ID: vid})
        session.flush = AsyncMock()

        with patch(
            "course_supporter.storage.repositories.SlideVideoMappingRepository"
        ) as repo_cls:
            repo_cls.return_value.find_pending_by_material = AsyncMock(
                return_value=[orm_mapping]
            )
            svc = MappingValidationService(session)
            await svc.revalidate_blocked(VID_ID)

        assert orm_mapping.validation_state == MappingValidationState.VALIDATED
        assert orm_mapping.validated_at is not None
