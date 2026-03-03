"""Shared file upload validation utilities.

Provides extension validation and allowed extensions mapping
used by both legacy course material uploads and node material uploads.
"""

from __future__ import annotations

from course_supporter.models.source import SourceType

# Allowed file extensions per source_type (lowercase, with dot).
# web does not accept file uploads at all.
ALLOWED_EXTENSIONS: dict[SourceType, frozenset[str]] = {
    SourceType.VIDEO: frozenset({".mp4", ".webm", ".mkv", ".avi"}),
    SourceType.PRESENTATION: frozenset({".pdf", ".pptx"}),
    SourceType.TEXT: frozenset(
        {".md", ".markdown", ".docx", ".html", ".htm", ".txt"},
    ),
}


def file_extension(filename: str | None) -> str:
    """Extract lowercase file extension from filename.

    Args:
        filename: Original filename or None.

    Returns:
        Extension with leading dot (e.g. ".pdf") or empty string.
    """
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", maxsplit=1)[-1].lower()
