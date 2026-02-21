"""Structural validation for slide-video mappings (Level 1).

Validates that entry IDs exist, belong to the correct node, have the
expected source_type, and that timecodes are well-formed before any
ORM objects are created.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

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


class MappingValidationService:
    """Structural (Level 1) validation for slide-video mappings."""

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

        Collects all errors (entry IDs + timecode formats) in one pass.
        Timecode ordering is checked only when both formats are valid.
        """
        errors: list[MappingValidationError] = []

        # ── Entry checks ──
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

        # ── Timecode format ──
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

        # ── Timecode ordering (only when both formats are valid) ──
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
