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
from course_supporter.storage.orm import MaterialEntry

_TIMECODE_RE = re.compile(r"^(\d{1,2}:)?\d{2}:\d{2}$")


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


class MappingValidationService:
    """Structural (Level 1) validation for slide-video mappings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def validate_structural(
        self,
        node_id: uuid.UUID,
        mapping: SlideVideoMapEntry,
    ) -> list[MappingValidationError]:
        """Run structural checks on a single mapping.

        Checks are sequential — first error per entry is sufficient.

        Returns:
            Empty list when mapping is valid; list with one error otherwise.
        """
        errors: list[MappingValidationError] = []

        # ── Presentation entry ──
        pres_err = await self._validate_entry(
            entry_id_str=mapping.presentation_entry_id,
            node_id=node_id,
            expected_type="presentation",
            field="presentation_entry_id",
        )
        if pres_err is not None:
            errors.append(pres_err)
            return errors

        # ── Video entry ──
        video_err = await self._validate_entry(
            entry_id_str=mapping.video_entry_id,
            node_id=node_id,
            expected_type="video",
            field="video_entry_id",
        )
        if video_err is not None:
            errors.append(video_err)
            return errors

        # ── Timecode format ──
        tc_start = mapping.video_timecode_start
        if not _TIMECODE_RE.match(tc_start):
            errors.append(
                MappingValidationError(
                    field="video_timecode_start",
                    message=f"Invalid timecode format '{tc_start}'",
                    hint="Use HH:MM:SS or MM:SS format (e.g., '01:23:45')",
                )
            )
            return errors

        tc_end = mapping.video_timecode_end
        if tc_end is not None:
            if not _TIMECODE_RE.match(tc_end):
                errors.append(
                    MappingValidationError(
                        field="video_timecode_end",
                        message=f"Invalid timecode format '{tc_end}'",
                        hint="Use HH:MM:SS or MM:SS format (e.g., '01:23:45')",
                    )
                )
                return errors

            # ── Timecode ordering ──
            if _timecode_to_seconds(tc_end) < _timecode_to_seconds(tc_start):
                errors.append(
                    MappingValidationError(
                        field="video_timecode_end",
                        message=(
                            f"timecode_end '{tc_end}' is before "
                            f"timecode_start '{tc_start}'"
                        ),
                        hint="video_timecode_end must be >= video_timecode_start",
                    )
                )
                return errors

        return errors

    # ── Private helpers ──

    async def _validate_entry(
        self,
        *,
        entry_id_str: str,
        node_id: uuid.UUID,
        expected_type: str,
        field: str,
    ) -> MappingValidationError | None:
        """Validate existence, ownership, and source_type for one entry."""
        entry_id = uuid.UUID(entry_id_str)
        stmt = select(MaterialEntry).where(MaterialEntry.id == entry_id)
        result = await self._session.execute(stmt)
        entry = result.scalar_one_or_none()

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
                    f"expected '{expected_type}'"
                ),
                hint=f"Use a {expected_type} material for {field}",
            )

        return None
