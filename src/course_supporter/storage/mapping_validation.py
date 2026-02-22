"""Structural, content, and deferred validation for slide-video mappings.

Level 1 (structural): entry IDs exist, belong to the correct node, have the
expected source_type, and timecodes are well-formed.

Level 2 (content): slide_number within presentation page_count, timecodes
within video duration. Requires processed_content to be available (READY
state). Skipped when material is not READY.

Level 3 (deferred): when a material is not READY (RAW/PENDING/ERROR/
INTEGRITY_BROKEN), the mapping is accepted with ``blocking_factors``
recorded and ``validation_state = pending_validation``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.models.course import SlideVideoMapEntry
from course_supporter.models.source import SourceType
from course_supporter.storage.orm import (
    MappingValidationState,
    MaterialEntry,
    MaterialState,
)

_TIMECODE_RE = re.compile(r"^([0-9]{1,2}:)?[0-5][0-9]:[0-5][0-9]$")

# ── Metadata key constants ──
_KEY_METADATA = "metadata"
_KEY_PAGE_COUNT = "page_count"
_KEY_CHUNKS = "chunks"
_KEY_END_SEC = "end_sec"


@dataclass
class MappingValidationError:
    """Single validation error with optional hint."""

    field: str
    message: str
    hint: str | None = None


@dataclass
class MappingBlockingFactor:
    """Reason why Level 2 validation was deferred for a material."""

    type: str  # "material_not_ready" | "material_error"
    material_entry_id: str  # UUID as string (JSON-safe)
    filename: str | None
    material_state: str  # entry.state.value
    message: str
    blocked_checks: list[str]


@dataclass
class MappingValidationResult:
    """Validation outcome for a single mapping in a batch."""

    index: int
    status: MappingValidationState
    errors: list[MappingValidationError]
    blocking_factors: list[MappingBlockingFactor]


# ── Blocker type constants ──
_BLOCKER_NOT_READY = "material_not_ready"
_BLOCKER_ERROR = "material_error"

_ERROR_STATES = frozenset({MaterialState.ERROR, MaterialState.INTEGRITY_BROKEN})


def _timecode_to_seconds(tc: str) -> int:
    """Convert HH:MM:SS or MM:SS string to total seconds."""
    parts = tc.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return int(parts[0]) * 60 + int(parts[1])


def _parse_uuid(value: str) -> uuid.UUID | None:
    """Parse a string as UUID, returning None on invalid format."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_processed_content(raw: str) -> dict[str, Any] | None:
    """Parse processed_content JSON string, returning None on failure."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _extract_page_count(doc: dict[str, Any]) -> int | None:
    """Extract page_count from SourceDocument metadata."""
    metadata = doc.get(_KEY_METADATA)
    if not isinstance(metadata, dict):
        return None
    page_count = metadata.get(_KEY_PAGE_COUNT)
    if isinstance(page_count, int) and page_count > 0:
        return page_count
    return None


def _extract_video_duration_sec(doc: dict[str, Any]) -> float | None:
    """Extract video duration as max end_sec across all chunks.

    Returns None when no chunks contain end_sec (fallback: skip check).
    """
    chunks = doc.get(_KEY_CHUNKS)
    if not isinstance(chunks, list) or not chunks:
        return None
    max_end: float | None = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get(_KEY_METADATA)
        if not isinstance(meta, dict):
            continue
        end_sec = meta.get(_KEY_END_SEC)
        if isinstance(end_sec, int | float) and (max_end is None or end_sec > max_end):
            max_end = end_sec
    return max_end


def _seconds_to_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS for display."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _check_slide_number(
    slide_number: int,
    doc: dict[str, Any],
) -> MappingValidationError | None:
    """Check slide_number is within presentation page_count."""
    page_count = _extract_page_count(doc)
    if page_count is None:
        return None
    if slide_number < 1 or slide_number > page_count:
        return MappingValidationError(
            field="slide_number",
            message=(
                f"Slide {slide_number} does not exist in presentation "
                f"({page_count} slides total)"
            ),
            hint=f"Allowed range: 1\u2013{page_count}",
        )
    return None


def _check_timecode_range(
    timecode_sec: int,
    timecode_str: str,
    field: str,
    duration: float,
) -> MappingValidationError | None:
    """Check timecode does not exceed video duration."""
    if timecode_sec > duration:
        return MappingValidationError(
            field=field,
            message=(
                f"Timecode '{timecode_str}' ({timecode_sec}s) exceeds "
                f"video duration ({_seconds_to_timecode(duration)})"
            ),
            hint=f"Allowed range: 00:00\u2013{_seconds_to_timecode(duration)}",
        )
    return None


class MappingValidationService:
    """Structural (Level 1) and content (Level 2) validation for mappings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def validate_batch(
        self,
        node_id: uuid.UUID,
        mappings: list[SlideVideoMapEntry],
    ) -> list[MappingValidationResult]:
        """Validate all mappings with a single DB query.

        Collects unique entry IDs, fetches them in one SELECT ... WHERE
        id IN (...), then validates each mapping in memory.

        Returns:
            A result for every mapping in the batch. Each result is
            classified as VALIDATED, PENDING_VALIDATION (has blockers
            but no errors), or VALIDATION_FAILED (has errors).
        """
        # ── Collect all entry IDs that parse as valid UUIDs ──
        all_ids: set[uuid.UUID] = set()
        for m in mappings:
            pres_id = _parse_uuid(m.presentation_entry_id)
            if pres_id is not None:
                all_ids.add(pres_id)
            vid_id = _parse_uuid(m.video_entry_id)
            if vid_id is not None:
                all_ids.add(vid_id)

        # ── Single query to fetch all referenced entries ──
        entries_by_id: dict[uuid.UUID, MaterialEntry] = {}
        if all_ids:
            stmt = select(MaterialEntry).where(MaterialEntry.id.in_(all_ids))
            result = await self._session.execute(stmt)
            for entry in result.scalars().all():
                entries_by_id[entry.id] = entry

        # ── Pre-parse processed_content once per entry for Level 2 ──
        parsed_docs: dict[uuid.UUID, dict[str, Any]] = {}
        for entry_id, entry in entries_by_id.items():
            if entry.processed_content is not None:
                doc = _parse_processed_content(entry.processed_content)
                if doc is not None:
                    parsed_docs[entry_id] = doc

        # ── Validate each mapping and classify ──
        results: list[MappingValidationResult] = []
        for idx, m in enumerate(mappings):
            errs, blockers = self._validate_single(
                node_id, m, entries_by_id, parsed_docs
            )
            if errs:
                status = MappingValidationState.VALIDATION_FAILED
            elif blockers:
                status = MappingValidationState.PENDING_VALIDATION
            else:
                status = MappingValidationState.VALIDATED
            results.append(
                MappingValidationResult(
                    index=idx,
                    status=status,
                    errors=errs,
                    blocking_factors=blockers,
                )
            )

        return results

    def _validate_single(
        self,
        node_id: uuid.UUID,
        mapping: SlideVideoMapEntry,
        entries_by_id: dict[uuid.UUID, MaterialEntry],
        parsed_docs: dict[uuid.UUID, dict[str, Any]],
    ) -> tuple[list[MappingValidationError], list[MappingBlockingFactor]]:
        """Validate a single mapping against pre-fetched entries.

        Runs Level 1 (structural) checks first. For entries that pass L1,
        checks material state: if not READY, creates a blocking factor and
        skips Level 2. Level 2 (content) runs only for READY entries.

        Returns:
            Tuple of (errors, blocking_factors).
        """
        errors: list[MappingValidationError] = []
        blockers: list[MappingBlockingFactor] = []

        # ── Level 1: Entry checks ──
        pres_err = self._check_entry(
            entry_id_str=mapping.presentation_entry_id,
            node_id=node_id,
            expected_type=SourceType.PRESENTATION,
            field="presentation_entry_id",
            entries_by_id=entries_by_id,
        )
        if pres_err is not None:
            errors.append(pres_err)

        video_err = self._check_entry(
            entry_id_str=mapping.video_entry_id,
            node_id=node_id,
            expected_type=SourceType.VIDEO,
            field="video_entry_id",
            entries_by_id=entries_by_id,
        )
        if video_err is not None:
            errors.append(video_err)

        # ── Level 1: Timecode format ──
        tc_start = mapping.video_timecode_start
        tc_start_valid = bool(_TIMECODE_RE.match(tc_start))
        if not tc_start_valid:
            errors.append(
                MappingValidationError(
                    field="video_timecode_start",
                    message=f"Invalid timecode format '{tc_start}'",
                    hint="Use HH:MM:SS or MM:SS format (e.g., '01:23:45')",
                )
            )

        tc_end = mapping.video_timecode_end
        tc_end_valid = True
        if tc_end is not None:
            tc_end_valid = bool(_TIMECODE_RE.match(tc_end))
            if not tc_end_valid:
                errors.append(
                    MappingValidationError(
                        field="video_timecode_end",
                        message=f"Invalid timecode format '{tc_end}'",
                        hint="Use HH:MM:SS or MM:SS format (e.g., '01:23:45')",
                    )
                )

        # ── Level 1: Timecode ordering (only when both formats are valid) ──
        if (
            tc_start_valid
            and tc_end_valid
            and tc_end is not None
            and _timecode_to_seconds(tc_end) < _timecode_to_seconds(tc_start)
        ):
            errors.append(
                MappingValidationError(
                    field="video_timecode_end",
                    message=(
                        f"timecode_end '{tc_end}' is before timecode_start '{tc_start}'"
                    ),
                    hint="video_timecode_end must be >= video_timecode_start",
                )
            )

        # ── Level 3: Check entry state, create blockers for non-READY ──
        pres_uuid = (
            _parse_uuid(mapping.presentation_entry_id) if pres_err is None else None
        )
        vid_uuid = _parse_uuid(mapping.video_entry_id) if video_err is None else None

        pres_ready = False
        if pres_uuid is not None:
            pres_entry = entries_by_id.get(pres_uuid)
            if pres_entry is not None and pres_entry.state != MaterialState.READY:
                blockers.append(
                    self._make_blocker(pres_entry, ["slide_number"]),
                )
            else:
                pres_ready = True

        vid_ready = False
        if vid_uuid is not None:
            vid_entry = entries_by_id.get(vid_uuid)
            if vid_entry is not None and vid_entry.state != MaterialState.READY:
                blockers.append(
                    self._make_blocker(
                        vid_entry,
                        ["video_timecode_start", "video_timecode_end"],
                    ),
                )
            else:
                vid_ready = True

        # ── Level 2: Content validation (only for READY entries) ──
        if pres_ready and pres_uuid is not None:
            pres_doc = parsed_docs.get(pres_uuid)
            if pres_doc is not None:
                slide_err = _check_slide_number(mapping.slide_number, pres_doc)
                if slide_err is not None:
                    errors.append(slide_err)

        if vid_ready and vid_uuid is not None:
            video_doc = parsed_docs.get(vid_uuid)
            if video_doc is not None:
                duration = _extract_video_duration_sec(video_doc)
                if duration is not None:
                    if tc_start_valid:
                        tc_start_err = _check_timecode_range(
                            _timecode_to_seconds(tc_start),
                            tc_start,
                            "video_timecode_start",
                            duration,
                        )
                        if tc_start_err is not None:
                            errors.append(tc_start_err)

                    if tc_end is not None and tc_end_valid:
                        tc_end_err = _check_timecode_range(
                            _timecode_to_seconds(tc_end),
                            tc_end,
                            "video_timecode_end",
                            duration,
                        )
                        if tc_end_err is not None:
                            errors.append(tc_end_err)

        return errors, blockers

    # ── Private helpers ──

    @staticmethod
    def _make_blocker(
        entry: MaterialEntry,
        blocked_checks: list[str],
    ) -> MappingBlockingFactor:
        """Create a MappingBlockingFactor from a non-READY entry."""
        state = entry.state
        blocker_type = _BLOCKER_ERROR if state in _ERROR_STATES else _BLOCKER_NOT_READY
        return MappingBlockingFactor(
            type=blocker_type,
            material_entry_id=str(entry.id),
            filename=entry.filename,
            material_state=state.value,
            message=(
                f"Material '{entry.id}' is in state '{state.value}', "
                f"content checks deferred"
            ),
            blocked_checks=blocked_checks,
        )

    @staticmethod
    def _check_entry(
        *,
        entry_id_str: str,
        node_id: uuid.UUID,
        expected_type: SourceType,
        field: str,
        entries_by_id: dict[uuid.UUID, MaterialEntry],
    ) -> MappingValidationError | None:
        """Validate existence, ownership, and source_type for one entry."""
        entry_id = _parse_uuid(entry_id_str)
        if entry_id is None:
            return MappingValidationError(
                field=field,
                message=f"Invalid UUID format '{entry_id_str}'",
                hint="Ensure the ID is a valid UUID string",
            )

        entry = entries_by_id.get(entry_id)

        if entry is None:
            return MappingValidationError(
                field=field,
                message=f"Entry '{entry_id}' not found",
                hint="Check that the entry ID is correct",
            )

        if entry.node_id != node_id:
            return MappingValidationError(
                field=field,
                message=(
                    f"Entry '{entry_id}' belongs to node "
                    f"'{entry.node_id}', not '{node_id}'"
                ),
                hint="Both entries must belong to the target node",
            )

        if entry.source_type != expected_type:
            return MappingValidationError(
                field=field,
                message=(
                    f"Entry '{entry_id}' is type '{entry.source_type}', "
                    f"expected '{expected_type.value}'"
                ),
                hint=f"Use a {expected_type.value} material for {field}",
            )

        return None
