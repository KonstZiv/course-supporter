"""Domain-specific exceptions for course-supporter."""


class NodeNotFoundError(Exception):
    """Raised when a MaterialNode is not found in the tree."""


class NoReadyMaterialsError(Exception):
    """Raised when no READY materials are available for generation."""
