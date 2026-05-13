"""Domain-specific exceptions for course-supporter."""

from __future__ import annotations


class NodeNotFoundError(Exception):
    """Raised when a CourseNode is not found in the tree."""


class NoReadyMaterialsError(Exception):
    """Raised when no READY materials are available for generation."""
