"""Tests for safe archive extraction."""

from __future__ import annotations

import gzip
import stat
import zipfile
from pathlib import Path

import pytest

from course_supporter.safety.archive import (
    _sanitize_filename,
    extract_submission_content,
)
from course_supporter.safety.exceptions import (
    ArchiveBombError,
    SymlinkViolationError,
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

    async def test_read_non_utf8_file_uses_replacement(self, tmp_dir: Path) -> None:
        """Non-UTF-8 bytes are replaced rather than decoded as latin-1."""
        f = tmp_dir / "notes.txt"
        f.write_bytes("caf\xe9 r\xe9sum\xe9".encode("latin-1"))
        result = await extract_submission_content(f)
        # latin-1 bytes are not silently decoded; invalid bytes become U+FFFD
        assert "caf" in result.files[0].content
        assert "\ufffd" in result.files[0].content


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

        with pytest.raises(ArchiveBombError, match="Nested archive"):
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

        with pytest.raises(ArchiveBombError, match="files") as exc_info:
            await extract_submission_content(zpath)
        assert exc_info.value.details["file_count"] == 5
        assert exc_info.value.details["max_files"] == 2

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

        with pytest.raises(ArchiveBombError, match="exceeds limit"):
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

        with pytest.raises(ArchiveBombError, match="exceeds limit"):
            await extract_submission_content(gz_path)


class TestSymlinkProtection:
    """Symlink attack prevention."""

    async def test_reject_symlink_input(self, tmp_dir: Path) -> None:
        """Input file that is a symlink is rejected."""
        real = tmp_dir / "real.py"
        real.write_text("x = 1", encoding="utf-8")
        link = tmp_dir / "link.py"
        link.symlink_to(real)

        with pytest.raises(SymlinkViolationError, match="symlink") as exc_info:
            await extract_submission_content(link)
        assert exc_info.value.violation_type == "symlink"
        assert exc_info.value.details["filename"] == "link.py"

    async def test_zip_symlink_entries_skipped_with_warning(
        self,
        tmp_dir: Path,
    ) -> None:
        """ZIP symlink entries are skipped and produce security warnings."""
        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("real.py", "x = 1")
            symlink_info = zipfile.ZipInfo("evil.py")
            symlink_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(symlink_info, "/etc/passwd")

        result = await extract_submission_content(zpath)
        assert len(result.files) == 1
        assert result.files[0].filename == "real.py"
        assert len(result.security_warnings) == 1
        assert result.security_warnings[0].violation_type == "symlink"
        assert result.security_warnings[0].raw_filename == "evil.py"


class TestPathSanitization:
    """Zip Slip and path traversal prevention."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("main.py", "main.py"),
            ("src/utils.py", "src/utils.py"),
            ("../../../etc/passwd", "etc/passwd"),
            ("foo/../../bar.py", "foo/bar.py"),
            ("/absolute/path.py", "absolute/path.py"),
        ],
    )
    def test_sanitize_filename(self, raw: str, expected: str) -> None:
        """Path traversal components are stripped."""
        assert _sanitize_filename(raw) == expected

    @pytest.mark.parametrize("raw", ["../", "..", "/../.."])
    def test_sanitize_filename_empty_result(self, raw: str) -> None:
        """Filenames that resolve to nothing return None."""
        assert _sanitize_filename(raw) is None

    async def test_zip_traversal_sanitized_with_warning(
        self,
        tmp_dir: Path,
    ) -> None:
        """ZIP entry with path traversal gets sanitized and produces warning."""
        zpath = tmp_dir / "hw.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("../../evil.py", "import os")
            zf.writestr("safe.py", "x = 1")

        result = await extract_submission_content(zpath)
        filenames = {f.filename for f in result.files}
        assert "evil.py" in filenames
        assert "../../evil.py" not in filenames
        assert "safe.py" in filenames
        # Path traversal produces a warning
        traversal_warnings = [
            w for w in result.security_warnings if w.violation_type == "path_traversal"
        ]
        assert len(traversal_warnings) == 1
        assert traversal_warnings[0].raw_filename == "../../evil.py"
        assert traversal_warnings[0].filename == "evil.py"


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
        assert text.strip() == "--- a.py ---\nx = 1\n--- b.py ---\ny = 2"
