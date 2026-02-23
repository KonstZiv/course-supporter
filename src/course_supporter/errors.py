"""Domain-specific exceptions for course-supporter."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from course_supporter.conflict_detection import ConflictInfo


class NodeNotFoundError(Exception):
    """Raised when a MaterialNode is not found in the tree."""


class NoReadyMaterialsError(Exception):
    """Raised when no READY materials are available for generation."""


class GenerationConflictError(Exception):
    """Active generation job overlaps with the requested scope."""

    def __init__(self, conflict: ConflictInfo) -> None:
        self.conflict = conflict
        super().__init__(f"Conflict with job {conflict.job_id}: {conflict.reason}")
