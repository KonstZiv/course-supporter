"""Structural and content validation for slide-video mappings (Levels 1-2).

Level 1 (structural): entry IDs exist, belong to the correct node, have the
expected source_type, and timecodes are well-formed.

Level 2 (content): slide_number within presentation page_count, timecodes
within video duration. Requires processed_content to be available (READY
state). Skipped silently when processed_content is absent.
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
from course_supporter.storage.orm import MaterialEntry

_TIMECODE_RE = re.compile(r"^([0-9]{1,2}:)?[0-5][0-9]:[0-5][0-9]$")


@dataclass
class MappingValidationError:
    """Single validation error with optional hint."""

    field: str
    message: str
    hint: str | None = None


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
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return None
    page_count = metadata.get("page_count")
    if isinstance(page_count, int) and page_count > 0:
        return page_count
    return None


def _extract_video_duration_sec(doc: dict[str, Any]) -> float | None:
    """Extract video duration as max end_sec across all chunks.

    Returns None when no chunks contain end_sec (fallback: skip check).
    """
    chunks = doc.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None
    max_end: float | None = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("metadata")
        if not isinstance(meta, dict):
            continue
        end_sec = meta.get("end_sec")
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
    processed_content: str,
) -> MappingValidationError | None:
    """Check slide_number is within presentation page_count."""
    doc = _parse_processed_content(processed_content)
    if doc is None:
        return None
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
    processed_content: str,
) -> MappingValidationError | None:
    """Check timecode does not exceed video duration."""
    doc = _parse_processed_content(processed_content)
    if doc is None:
        return None
    duration = _extract_video_duration_sec(doc)
    if duration is None:
        return None
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
    ) -> list[tuple[int, list[MappingValidationError]]]:
        """Validate all mappings with a single DB query.

        Collects unique entry IDs, fetches them in one SELECT ... WHERE
        id IN (...), then validates each mapping in memory.

        Returns:
            List of (index, errors) tuples for mappings that failed
            validation. Empty list means all mappings are valid.
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

        # ── Validate each mapping in memory ──
        errors_per_mapping: list[tuple[int, list[MappingValidationError]]] = []
        for idx, m in enumerate(mappings):
            errs = self._validate_single(node_id, m, entries_by_id)
            if errs:
                errors_per_mapping.append((idx, errs))

        return errors_per_mapping

    def _validate_single(
        self,
        node_id: uuid.UUID,
        mapping: SlideVideoMapEntry,
        entries_by_id: dict[uuid.UUID, MaterialEntry],
    ) -> list[MappingValidationError]:
        """Validate a single mapping against pre-fetched entries.

        Runs Level 1 (structural) checks first, then Level 2 (content)
        checks when the referenced entries have processed_content.
        Collects all errors in one pass.
        """
        errors: list[MappingValidationError] = []

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

        # ── Level 2: Content validation (when entries are structurally OK) ──
        pres_entry = (
            entries_by_id.get(uuid.UUID(mapping.presentation_entry_id))
            if pres_err is None
            else None
        )
        video_entry = (
            entries_by_id.get(uuid.UUID(mapping.video_entry_id))
            if video_err is None
            else None
        )

        if pres_entry is not None and pres_entry.processed_content is not None:
            slide_err = _check_slide_number(
                mapping.slide_number, pres_entry.processed_content
            )
            if slide_err is not None:
                errors.append(slide_err)

        if video_entry is not None and video_entry.processed_content is not None:
            if tc_start_valid:
                tc_start_err = _check_timecode_range(
                    _timecode_to_seconds(tc_start),
                    tc_start,
                    "video_timecode_start",
                    video_entry.processed_content,
                )
                if tc_start_err is not None:
                    errors.append(tc_start_err)

            if tc_end is not None and tc_end_valid:
                tc_end_err = _check_timecode_range(
                    _timecode_to_seconds(tc_end),
                    tc_end,
                    "video_timecode_end",
                    video_entry.processed_content,
                )
                if tc_end_err is not None:
                    errors.append(tc_end_err)

        return errors

    # ── Private helpers ──

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
