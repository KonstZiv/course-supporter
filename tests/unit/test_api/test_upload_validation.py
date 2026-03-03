"""Tests for shared upload validation utilities."""

from __future__ import annotations

from course_supporter.api.upload_validation import (
    ALLOWED_EXTENSIONS,
    file_extension,
)
from course_supporter.models.source import SourceType


class TestFileExtension:
    """file_extension() extracts lowercase extension."""

    def test_pdf(self) -> None:
        assert file_extension("slides.pdf") == ".pdf"

    def test_uppercase(self) -> None:
        assert file_extension("VIDEO.MP4") == ".mp4"

    def test_no_extension(self) -> None:
        assert file_extension("videofile") == ""

    def test_none(self) -> None:
        assert file_extension(None) == ""

    def test_empty(self) -> None:
        assert file_extension("") == ""

    def test_double_dot(self) -> None:
        assert file_extension("archive.tar.gz") == ".gz"


class TestAllowedExtensions:
    """ALLOWED_EXTENSIONS contains expected values."""

    def test_video_extensions(self) -> None:
        assert ".mp4" in ALLOWED_EXTENSIONS[SourceType.VIDEO]
        assert ".webm" in ALLOWED_EXTENSIONS[SourceType.VIDEO]

    def test_presentation_extensions(self) -> None:
        assert ".pdf" in ALLOWED_EXTENSIONS[SourceType.PRESENTATION]
        assert ".pptx" in ALLOWED_EXTENSIONS[SourceType.PRESENTATION]

    def test_text_extensions(self) -> None:
        assert ".md" in ALLOWED_EXTENSIONS[SourceType.TEXT]
        assert ".docx" in ALLOWED_EXTENSIONS[SourceType.TEXT]
        assert ".txt" in ALLOWED_EXTENSIONS[SourceType.TEXT]

    def test_web_not_in_allowed(self) -> None:
        assert SourceType.WEB not in ALLOWED_EXTENSIONS
