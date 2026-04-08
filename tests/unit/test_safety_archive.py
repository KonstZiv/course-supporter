"""Tests for safe archive extraction."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import pytest

from course_supporter.safety.archive import (
    ArchiveExtractionError,
    extract_submission_content,
)


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestSingleFile:
    """Single text file extraction."""

    async def test_read_python_file(self, tmp_dir: Path) -> None:
        """Reads a .py file as text."""
        f = tmp_dir / "solution.py"
        f.write_text("x = 1\nprint(x)\n", encoding="utf-8")
        result = await extract_submission_content(f)
        assert len(result.files) == 1
        assert result.files[0].filename == "solution.py"
        assert "x = 1" in result.files[0].content

    async def test_read_latin1_file(self, tmp_dir: Path) -> None:
        """Reads a file with latin-1 encoding."""
        f = tmp_dir / "notes.txt"
        f.write_bytes("café résumé".encode("latin-1"))
        result = await extract_submission_content(f)
        assert "caf" in result.files[0].content


class TestZipExtraction:
    """ZIP archive extraction with bomb protection."""

    async def test_extract_zip_with_code(self, tmp_dir: Path) -> None:
        """Extracts text files from a ZIP archive."""
        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("main.py", "print('hello')")
            zf.writestr("utils.py", "def add(a, b): return a + b")
        result = await extract_submission_content(zpath)
        assert len(result.files) == 2
        filenames = {f.filename for f in result.files}
        assert "main.py" in filenames
        assert "utils.py" in filenames

    async def test_skip_non_text_files(self, tmp_dir: Path) -> None:
        """Non-text files in ZIP are skipped."""
        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("code.py", "x = 1")
            zf.writestr("image.png", b"\x89PNG\r\n".decode("latin-1"))
        result = await extract_submission_content(zpath)
        assert len(result.files) == 1
        assert result.files[0].filename == "code.py"

    async def test_nested_archive_rejected(self, tmp_dir: Path) -> None:
        """ZIP containing another ZIP is rejected (nesting=1)."""
        inner = tmp_dir / "inner.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("a.py", "x = 1")

        outer = tmp_dir / "outer.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(inner, "inner.zip")

        with pytest.raises(ArchiveExtractionError, match="Nested archive"):
            await extract_submission_content(outer)

    async def test_too_many_files_rejected(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ZIP with too many files is rejected."""
        from course_supporter.safety import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 50
            safety_archive_max_files = 2
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for i in range(5):
                zf.writestr(f"file{i}.py", f"x = {i}")

        with pytest.raises(ArchiveExtractionError, match="files"):
            await extract_submission_content(zpath)

    async def test_oversized_uncompressed_rejected(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ZIP with oversized uncompressed content is rejected."""
        from course_supporter.safety import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 1  # 1 MB
            safety_archive_max_files = 1000
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("big.py", "x" * (2 * 1024 * 1024))  # 2 MB

        with pytest.raises(ArchiveExtractionError, match="exceeds limit"):
            await extract_submission_content(zpath)


class TestGzExtraction:
    """Gzip file extraction."""

    async def test_extract_gz(self, tmp_dir: Path) -> None:
        """Decompresses a .gz file."""
        gz_path = tmp_dir / "solution.py.gz"
        content = b"print('hello from gz')"
        with gzip.open(gz_path, "wb") as f:
            f.write(content)
        result = await extract_submission_content(gz_path)
        assert len(result.files) == 1
        assert "hello from gz" in result.files[0].content
        assert result.files[0].filename == "solution.py"

    async def test_oversized_gz_rejected(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Oversized gzip content is rejected."""
        from course_supporter.safety import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 1  # 1 MB
            safety_archive_max_files = 1000
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        gz_path = tmp_dir / "big.py.gz"
        with gzip.open(gz_path, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))

        with pytest.raises(ArchiveExtractionError, match="exceeds limit"):
            await extract_submission_content(gz_path)


class TestSubmissionContent:
    """SubmissionContent model tests."""

    async def test_full_text_concatenation(self, tmp_dir: Path) -> None:
        """full_text concatenates all file contents with headers."""
        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("a.py", "x = 1")
            zf.writestr("b.py", "y = 2")
        result = await extract_submission_content(zpath)
        text = result.full_text
        assert "--- a.py ---" in text
        assert "--- b.py ---" in text
        assert "x = 1" in text
        assert "y = 2" in text
