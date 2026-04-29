"""Tests for magic bytes file type detection (KD14, KD-B).

PNG / JPEG / GIF / ZIP fixtures are generated inline at module load
via Pillow / stdlib zipfile because libmagic 5.x requires real
structural content beyond the magic-byte signature -- bare 4-8 byte
headers plus zero padding fall through to ``application/octet-stream``.
PDF and PE/COFF headers are recognized from the prefix alone, so
those stay as inline byte literals.

No on-disk fixtures: callers operate on bytes, generation runs in
microseconds at import time.
"""

import io
import zipfile

import pytest
from PIL import Image

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.file_type import (
    detect_charset,
    detect_mime_type,
    verify_extension_matches_content,
)


def _make_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 255, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_gif() -> bytes:
    buf = io.BytesIO()
    Image.new("P", (8, 8), 0).save(buf, format="GIF")
    return buf.getvalue()


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.txt", "hello")
    return buf.getvalue()


# Inline magic for formats libmagic recognizes from the header alone.
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"trailer body\n" * 10
PE_BYTES = b"\x4d\x5a\x90\x00\x03\x00\x00\x00" + b"\x00" * 200

# Real-structure fixtures generated via Pillow / zipfile.
PNG_BYTES = _make_png()
JPEG_BYTES = _make_jpeg()
GIF_BYTES = _make_gif()
ZIP_BYTES = _make_zip()


# ── detect_mime_type ───────────────────────────────────────────────


class TestDetectMimeType:
    def test_pdf_detected(self) -> None:
        assert detect_mime_type(PDF_BYTES).startswith("application/pdf")

    def test_png_detected(self) -> None:
        assert detect_mime_type(PNG_BYTES) == "image/png"

    def test_zip_detected(self) -> None:
        assert detect_mime_type(ZIP_BYTES) == "application/zip"

    def test_jpeg_detected(self) -> None:
        assert detect_mime_type(JPEG_BYTES) == "image/jpeg"

    def test_gif_detected(self) -> None:
        assert detect_mime_type(GIF_BYTES) == "image/gif"

    def test_plain_text_detected_as_text(self) -> None:
        text = b"Hello, world.\nThis is a plain text file.\n"
        assert detect_mime_type(text).startswith("text/")

    def test_pe_binary_not_classified_as_pdf(self) -> None:
        # Windows PE executable header — must not be detected as PDF.
        result = detect_mime_type(PE_BYTES)
        assert result.startswith("application/")
        assert "pdf" not in result.lower()

    def test_empty_returns_x_empty(self) -> None:
        assert detect_mime_type(b"") == "application/x-empty"


# ── detect_charset ─────────────────────────────────────────────────


class TestDetectCharset:
    def test_ascii_text(self) -> None:
        assert detect_charset(b"hello world") in {"us-ascii", "ascii"}

    def test_utf8_with_cyrillic(self) -> None:
        text = "Привіт, світе!".encode()
        assert detect_charset(text) == "utf-8"

    def test_binary_returns_none(self) -> None:
        assert detect_charset(PNG_BYTES) is None

    def test_empty_returns_none(self) -> None:
        assert detect_charset(b"") is None

    def test_pe_binary_returns_none(self) -> None:
        assert detect_charset(PE_BYTES) is None


# ── verify_extension_matches_content — accepts ─────────────────────


class TestExtensionMatchesAccepts:
    def test_pdf_extension_with_pdf_content(self) -> None:
        verify_extension_matches_content("homework.pdf", PDF_BYTES)

    def test_png_extension_with_png_content(self) -> None:
        verify_extension_matches_content("diagram.png", PNG_BYTES)

    def test_jpeg_extension_with_jpeg_content(self) -> None:
        verify_extension_matches_content("photo.jpg", JPEG_BYTES)

    def test_gif_extension_with_gif_content(self) -> None:
        verify_extension_matches_content("animation.gif", GIF_BYTES)

    def test_txt_extension_with_real_text(self) -> None:
        verify_extension_matches_content(
            "homework.txt", b"This is my homework. I solved problem 1.\n"
        )

    def test_zip_extension_with_zip_content(self) -> None:
        verify_extension_matches_content("archive.zip", ZIP_BYTES)

    def test_docx_extension_with_zip_content(self) -> None:
        # .docx is structurally a ZIP; libmagic returns either the
        # office MIME or application/zip depending on version.
        verify_extension_matches_content("report.docx", ZIP_BYTES)

    def test_uppercase_extension_normalised(self) -> None:
        verify_extension_matches_content("HOMEWORK.PDF", PDF_BYTES)

    def test_mixed_case_extension(self) -> None:
        verify_extension_matches_content("homework.Pdf", PDF_BYTES)

    def test_unicode_filename_with_pdf(self) -> None:
        verify_extension_matches_content("домашка.pdf", PDF_BYTES)

    def test_full_width_extension_normalises(self) -> None:
        # NFKC collapses full-width Latin (U+FF30 U+FF24 U+FF26) to
        # ASCII "PDF" before extension parsing.
        full_width_pdf = "ＰＤＦ"
        verify_extension_matches_content(f"homework.{full_width_pdf}", PDF_BYTES)


# ── verify_extension_matches_content — rejects ─────────────────────


class TestExtensionMatchesRejects:
    def test_pdf_extension_with_pe_binary(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("fake.pdf", PE_BYTES)
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    def test_txt_extension_with_pdf_content(self) -> None:
        # Binary masquerading as text — must be rejected.
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("fake.txt", PDF_BYTES)
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    def test_png_extension_with_pdf_content(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("diagram.png", PDF_BYTES)
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    def test_pdf_extension_with_png_content(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("fake.pdf", PNG_BYTES)
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    def test_empty_content(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("empty.txt", b"")
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH
        assert "empty" in exc_info.value.detail.lower()

    def test_no_extension(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("homework", b"some content")
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE

    def test_unrecognised_extension(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("malware.exe", PE_BYTES)
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE

    def test_dotfile_treated_as_no_extension(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content(".bashrc", b"some content")
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE


# ── Error detail surface ───────────────────────────────────────────


class TestErrorDetails:
    def test_mismatch_detail_includes_extension(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("fake.pdf", PE_BYTES)
        assert "pdf" in exc_info.value.detail.lower()

    def test_mismatch_detail_includes_detected_mime(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("fake.txt", PDF_BYTES)
        # Detail names the expected family OR detected MIME so logs
        # can correlate the mismatch.
        detail = exc_info.value.detail.lower()
        assert "pdf" in detail or "application" in detail

    def test_no_extension_detail_carries_filename(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("noext", b"content")
        assert "noext" in exc_info.value.detail

    def test_unrecognised_extension_detail_carries_extension(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            verify_extension_matches_content("malware.exe", PE_BYTES)
        assert "exe" in exc_info.value.detail.lower()
