"""Tests for safe archive extraction (KD14, KD-A).

Test fixtures generated synthetically via stdlib zipfile / tarfile /
gzip / io.BytesIO at module import. No on-disk corpus checked in --
GitHub security scanners flag archive bombs, and synthetic generation
keeps test inputs explicit and version-controlled at the source level.

Attack vectors covered:

* ZIP bombs (compression ratio + declared total + per-entry cap)
* tar.gz bombs (total ratio + per-entry cap + total declared)
* Path traversal (``..``, absolute, backslash, null byte)
* Symlink / hard-link / device / FIFO (TAR types other than regular)
* Directory depth (``> _MAX_DIRECTORY_DEPTH``)
* Archive-within-archive recursion depth
* Whitelist propagation through recursion
* Extension/content mismatch inside archive
* Empty archive
"""

import io
import tarfile
import zipfile

import pytest

from course_supporter.security.archive import (
    ExtractedFile,
    _validate_arcname,
    extract_archive_safely,
)
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)

# ── Fixture generators ─────────────────────────────────────────────


def _zip_fixture(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _tar_gz_fixture(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _tar_gz_with_special_member(
    name: str, member_type: bytes, *, linkname: str = ""
) -> bytes:
    """Build tar.gz containing a single non-regular member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=name)
        info.type = member_type
        info.linkname = linkname
        info.size = 0
        tf.addfile(info)
    return buf.getvalue()


def _nested_zip(depth: int, leaf: bytes, *, leaf_name: str = "leaf.txt") -> bytes:
    """Wrap ``leaf`` in ``depth`` levels of nested zip containers.

    ``depth=1`` yields a zip with ``leaf_name`` directly. ``depth=2``
    yields a zip whose only entry is ``nested.zip`` -- a zip with
    ``leaf_name``. And so on.
    """
    current = leaf
    current_name = leaf_name
    for _ in range(depth):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(current_name, current)
        current = buf.getvalue()
        current_name = "nested.zip"
    return current


# Real PDF magic bytes for content-match assertions inside archives.
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"trailer body\n" * 5
PE_BYTES = b"\x4d\x5a\x90\x00\x03\x00\x00\x00" + b"\x00" * 200

# Default whitelist for the bulk of tests.
_DEFAULT_WHITELIST = frozenset({"txt", "pdf", "zip"})
_DEFAULT_BUDGET = 1 * 1024 * 1024  # 1 MB


def _extract(
    archive_bytes: bytes,
    *,
    archive_kind: str = "zip",
    max_unzipped_size: int = _DEFAULT_BUDGET,
    max_nesting_depth: int = 3,
    allowed_extensions: frozenset[str] = _DEFAULT_WHITELIST,
) -> list[ExtractedFile]:
    """Eager-list helper for exhaustive iterator consumption."""
    return list(
        extract_archive_safely(
            archive_bytes,
            archive_kind=archive_kind,  # type: ignore[arg-type]
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
        )
    )


# ── Simple accept ──────────────────────────────────────────────────


class TestArchiveSimpleAccept:
    def test_simple_zip_yields_files(self) -> None:
        archive = _zip_fixture(
            [
                ("readme.txt", b"hello, world\n"),
                ("paper.pdf", PDF_BYTES),
            ]
        )
        files = _extract(archive)
        assert {f.arcname for f in files} == {"readme.txt", "paper.pdf"}
        assert all(f.depth == 0 for f in files)

    def test_simple_tar_gz_yields_files(self) -> None:
        archive = _tar_gz_fixture(
            [
                ("readme.txt", b"hello, world\n"),
                ("paper.pdf", PDF_BYTES),
            ]
        )
        files = _extract(archive, archive_kind="tar.gz")
        assert {f.arcname for f in files} == {"readme.txt", "paper.pdf"}

    def test_empty_zip_yields_nothing(self) -> None:
        archive = _zip_fixture([])
        assert _extract(archive) == []

    def test_empty_tar_gz_yields_nothing(self) -> None:
        archive = _tar_gz_fixture([])
        assert _extract(archive, archive_kind="tar.gz") == []

    def test_zip_directory_entry_skipped(self) -> None:
        archive = _zip_fixture(
            [
                ("dir/", b""),
                ("dir/leaf.txt", b"content"),
            ]
        )
        files = _extract(archive)
        assert {f.arcname for f in files} == {"dir/leaf.txt"}

    def test_zip_unicode_arcname_preserved(self) -> None:
        archive = _zip_fixture([("домашка.txt", "привіт".encode())])
        files = _extract(archive)
        assert files[0].arcname == "домашка.txt"


# ── ZIP bomb defense ───────────────────────────────────────────────


class TestZipBombDefense:
    def test_compression_ratio_per_entry_rejected(self) -> None:
        # 1 MB of zeros compresses to ~1 KB → ratio ~1000x.
        archive = _zip_fixture(
            [("bomb.txt", b"\x00" * (1024 * 1024))],
            compression=zipfile.ZIP_DEFLATED,
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(
                archive,
                max_unzipped_size=10 * 1024 * 1024,
                allowed_extensions=frozenset({"txt"}),
            )
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "ratio" in exc_info.value.detail.lower()

    def test_declared_total_exceeds_budget(self) -> None:
        # Two 600KB entries declared; total > 1MB budget.
        archive = _zip_fixture(
            [
                ("a.txt", b"x" * (600 * 1024)),
                ("b.txt", b"y" * (600 * 1024)),
            ],
            compression=zipfile.ZIP_STORED,
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, allowed_extensions=frozenset({"txt"}))
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "budget" in exc_info.value.detail.lower()

    def test_per_entry_cap_rejected(self) -> None:
        # Single entry consumes more than half the budget.
        archive = _zip_fixture(
            [("dominant.txt", b"x" * (700 * 1024))],
            compression=zipfile.ZIP_STORED,
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, allowed_extensions=frozenset({"txt"}))
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "per-entry" in exc_info.value.detail.lower()


# ── tar.gz bomb defense ────────────────────────────────────────────


class TestTarGzBombDefense:
    def test_total_compression_ratio_rejected(self) -> None:
        # 2 MB of zeros compresses extremely well in tar.gz.
        archive = _tar_gz_fixture([("bomb.txt", b"\x00" * (2 * 1024 * 1024))])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(
                archive,
                archive_kind="tar.gz",
                max_unzipped_size=10 * 1024 * 1024,
                allowed_extensions=frozenset({"txt"}),
            )
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_per_entry_cap_rejected(self) -> None:
        archive = _tar_gz_fixture([("dominant.txt", b"x" * (700 * 1024))])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(
                archive,
                archive_kind="tar.gz",
                allowed_extensions=frozenset({"txt"}),
            )
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION


# ── Path traversal ─────────────────────────────────────────────────


class TestPathTraversal:
    def test_dotdot_segment_in_zip_rejected(self) -> None:
        archive = _zip_fixture([("../etc/passwd", b"haha")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "traversal" in exc_info.value.detail.lower()

    def test_dotdot_segment_middle_in_zip_rejected(self) -> None:
        archive = _zip_fixture([("foo/../bar.txt", b"haha")])
        with pytest.raises(SecurityRejectedError):
            _extract(archive)

    def test_absolute_path_in_zip_rejected(self) -> None:
        archive = _zip_fixture([("/etc/passwd", b"haha")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive)
        assert "absolute" in exc_info.value.detail.lower()

    def test_backslash_in_zip_rejected(self) -> None:
        # zipfile preserves backslash in filename; archive.py rejects.
        archive = _zip_fixture([("evil\\path.txt", b"haha")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive)
        assert "backslash" in exc_info.value.detail.lower()

    def test_dotdot_in_tar_gz_rejected(self) -> None:
        archive = _tar_gz_fixture([("../etc/passwd", b"haha")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, archive_kind="tar.gz")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION


# ── Direct arcname validator ───────────────────────────────────────


class TestValidateArcname:
    """Unit tests for ``_validate_arcname`` covering edge cases that
    stdlib zipfile / tarfile silently strip during write (null bytes,
    some control sequences). Locks the defense behavior so it remains
    correct against archives constructed via lower-level libraries
    or hand-crafted bytes that bypass stdlib's normalization.
    """

    def test_null_byte_rejected(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _validate_arcname("file\x00.txt")
        assert "null" in exc_info.value.detail.lower()

    def test_empty_rejected(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _validate_arcname("")
        assert "empty" in exc_info.value.detail.lower()

    def test_dotdot_segment_rejected(self) -> None:
        with pytest.raises(SecurityRejectedError):
            _validate_arcname("../etc/passwd")

    def test_absolute_rejected(self) -> None:
        with pytest.raises(SecurityRejectedError):
            _validate_arcname("/etc/passwd")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(SecurityRejectedError):
            _validate_arcname("evil\\path.txt")

    def test_clean_arcname_returned(self) -> None:
        assert _validate_arcname("dir/leaf.txt") == "dir/leaf.txt"

    def test_unicode_arcname_normalized(self) -> None:
        # NFKC collapses full-width to ASCII before return.
        result = _validate_arcname("ＨＯＭＥ.txt")
        assert result == "HOME.txt"


# ── Symlink / non-regular tar entries ──────────────────────────────


class TestSymlinkRejection:
    def test_symlink_in_tar_gz_rejected(self) -> None:
        archive = _tar_gz_with_special_member(
            "link", tarfile.SYMTYPE, linkname="/etc/passwd"
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, archive_kind="tar.gz")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "non-regular" in exc_info.value.detail.lower()

    def test_hardlink_in_tar_gz_rejected(self) -> None:
        archive = _tar_gz_with_special_member(
            "hardlink", tarfile.LNKTYPE, linkname="other"
        )
        with pytest.raises(SecurityRejectedError):
            _extract(archive, archive_kind="tar.gz")

    def test_fifo_in_tar_gz_rejected(self) -> None:
        archive = _tar_gz_with_special_member("pipe", tarfile.FIFOTYPE)
        with pytest.raises(SecurityRejectedError):
            _extract(archive, archive_kind="tar.gz")

    def test_chrdev_in_tar_gz_rejected(self) -> None:
        archive = _tar_gz_with_special_member("char", tarfile.CHRTYPE)
        with pytest.raises(SecurityRejectedError):
            _extract(archive, archive_kind="tar.gz")


# ── Nesting depth ──────────────────────────────────────────────────


class TestNestingDepth:
    def test_directory_depth_at_limit_accepted(self) -> None:
        # depth = 8 (eight slashes after rstrip).
        deep = "a/b/c/d/e/f/g/h/leaf.txt"
        archive = _zip_fixture([(deep, b"hi")])
        files = _extract(archive)
        assert files[0].arcname == deep

    def test_directory_depth_exceeded_rejected(self) -> None:
        # depth = 9 → reject.
        too_deep = "a/b/c/d/e/f/g/h/i/leaf.txt"
        archive = _zip_fixture([(too_deep, b"hi")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive)
        assert "directory depth" in exc_info.value.detail.lower()

    def test_recursion_depth_3_accepted(self) -> None:
        # 3-level nesting: zip → zip → zip → leaf.txt.
        nested = _nested_zip(depth=3, leaf=b"hello")
        files = _extract(
            nested,
            max_nesting_depth=3,
            allowed_extensions=frozenset({"txt"}),
        )
        assert len(files) == 1
        assert files[0].depth == 2
        assert files[0].content == b"hello"

    def test_recursion_depth_4_rejected(self) -> None:
        nested = _nested_zip(depth=4, leaf=b"hello")
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(
                nested,
                max_nesting_depth=3,
                allowed_extensions=frozenset({"txt"}),
            )
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "nesting" in exc_info.value.detail.lower()


# ── Recursive extraction ───────────────────────────────────────────


class TestRecursiveExtraction:
    def test_zip_in_zip_extracted_at_depth_1(self) -> None:
        nested = _nested_zip(depth=2, leaf=b"deep content")
        files = _extract(
            nested,
            allowed_extensions=frozenset({"txt"}),
        )
        assert len(files) == 1
        assert files[0].depth == 1
        assert files[0].content == b"deep content"

    def test_recursive_whitelist_violation_propagates(self) -> None:
        # Inner zip contains .exe — outer caller's whitelist must reject.
        inner = _zip_fixture([("malware.exe", PE_BYTES)])
        outer = _zip_fixture([("nested.zip", inner)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(outer, allowed_extensions=frozenset({"txt", "zip"}))
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE
        assert "exe" in exc_info.value.detail.lower()


# ── Whitelist + content match ──────────────────────────────────────


class TestWhitelist:
    def test_disallowed_extension_rejected(self) -> None:
        archive = _zip_fixture([("malware.exe", PE_BYTES)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, allowed_extensions=frozenset({"txt", "pdf"}))
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE
        assert "exe" in exc_info.value.detail.lower()

    def test_no_extension_rejected(self) -> None:
        archive = _zip_fixture([("noext", b"some content")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, allowed_extensions=frozenset({"txt"}))
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE

    def test_extension_content_mismatch_rejected(self) -> None:
        # arcname "evil.pdf" but content is PE binary.
        archive = _zip_fixture([("evil.pdf", PE_BYTES)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive, allowed_extensions=frozenset({"pdf", "txt"}))
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH


# ── Malformed archives ─────────────────────────────────────────────


class TestMalformedArchive:
    def test_malformed_zip_bytes(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(b"not a zip at all")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "malformed" in exc_info.value.detail.lower()

    def test_malformed_tar_gz_bytes(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(b"not a tar.gz", archive_kind="tar.gz")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_unsupported_archive_kind(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(b"x", archive_kind="rar")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "unsupported" in exc_info.value.detail.lower()


# ── Phase 2.1 C2: extract_submission_content (KD-2.1-H) ────────────
#
# Tests for canonical submission-extraction migrated from
# safety/archive.py. Verify functional parity з legacy + canonical
# SecurityRejectedError raises with ErrorCategory ARCHIVE_BOMB /
# SYMLINK_VIOLATION values (per KD-2.1-I 2-set ratify).

import gzip  # noqa: E402
from pathlib import Path  # noqa: E402

from course_supporter.security.archive import (  # noqa: E402
    extract_submission_content,
)


def _write_zip_to_path(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """Write a ZIP archive to ``path`` with the given entries."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)


def _write_gz_to_path(path: Path, content: bytes) -> None:
    """Write a gzip file to ``path`` with the given content."""
    with gzip.open(path, "wb") as f:
        f.write(content)


class TestExtractSubmissionContentSingleFile:
    """extract_submission_content on a standalone text file."""

    @pytest.mark.asyncio
    async def test_reads_single_python_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "main.py"
        file_path.write_text("print('hello')\n", encoding="utf-8")

        result = await extract_submission_content(file_path)
        assert len(result.files) == 1
        assert result.files[0].filename == "main.py"
        assert result.files[0].content == "print('hello')\n"
        assert result.total_size == result.files[0].size
        assert result.security_warnings == []

    @pytest.mark.asyncio
    async def test_reads_single_markdown_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "README.md"
        file_path.write_text("# Title\n\nBody.\n", encoding="utf-8")

        result = await extract_submission_content(file_path)
        assert result.files[0].filename == "README.md"
        assert "# Title" in result.files[0].content


class TestExtractSubmissionContentZip:
    """extract_submission_content on .zip archives."""

    @pytest.mark.asyncio
    async def test_extracts_text_entries(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "submission.zip"
        _write_zip_to_path(
            archive_path,
            [
                ("solution.py", b"def f(): return 42\n"),
                ("README.md", b"# Solution\n"),
            ],
        )

        result = await extract_submission_content(archive_path)
        assert len(result.files) == 2
        filenames = {f.filename for f in result.files}
        assert filenames == {"solution.py", "README.md"}
        assert result.security_warnings == []

    @pytest.mark.asyncio
    async def test_skips_non_text_entries(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "mixed.zip"
        _write_zip_to_path(
            archive_path,
            [
                ("code.py", b"x = 1\n"),
                ("image.png", b"\x89PNG\r\n\x1a\n"),
                ("notes.md", b"# Notes\n"),
            ],
        )

        result = await extract_submission_content(archive_path)
        filenames = {f.filename for f in result.files}
        assert filenames == {"code.py", "notes.md"}
        # image.png skipped silently (not a SecurityWarning — it's debug log).

    @pytest.mark.asyncio
    async def test_path_traversal_sanitized_with_warning(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "traversal.zip"
        _write_zip_to_path(
            archive_path,
            [("../etc/passwd.py", b"# evil\n")],
        )

        result = await extract_submission_content(archive_path)
        # Path traversal sanitized — filename stripped to "etc/passwd.py"
        assert any(
            w.violation_type == "path_traversal" for w in result.security_warnings
        )
        # File still extracted under safe name (after sanitization).
        safe_filenames = {f.filename for f in result.files}
        assert "etc/passwd.py" in safe_filenames


class TestExtractSubmissionContentGz:
    """extract_submission_content on .gz files."""

    @pytest.mark.asyncio
    async def test_decompresses_gz(self, tmp_path: Path) -> None:
        gz_path = tmp_path / "log.txt.gz"
        _write_gz_to_path(gz_path, b"line one\nline two\n")

        result = await extract_submission_content(gz_path)
        assert len(result.files) == 1
        assert result.files[0].filename == "log.txt"  # stem (.gz stripped)
        assert result.files[0].content == "line one\nline two\n"


class TestExtractSubmissionContentSymlink:
    """extract_submission_content rejects symlinks (SYMLINK_VIOLATION)."""

    @pytest.mark.asyncio
    async def test_symlink_raises_canonical_error(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("safe content\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(link)
        assert exc_info.value.category is ErrorCategory.SYMLINK_VIOLATION
        assert "symlink" in exc_info.value.detail.lower()


class TestExtractSubmissionContentArchiveBomb:
    """extract_submission_content rejects bomb patterns (ARCHIVE_BOMB)."""

    @pytest.mark.asyncio
    async def test_nested_archive_raises_bomb(self, tmp_path: Path) -> None:
        # Nested .zip inside .zip — triggers bomb check (max_nesting=1).
        inner_path = tmp_path / "inner.zip"
        _write_zip_to_path(inner_path, [("payload.py", b"x = 1\n")])

        outer_path = tmp_path / "outer.zip"
        with zipfile.ZipFile(outer_path, "w") as zf:
            zf.write(inner_path, arcname="nested.zip")

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(outer_path)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_BOMB
        assert "nested" in exc_info.value.detail.lower()


class TestExtractSubmissionContentMalformed:
    """extract_submission_content raises SecurityRejectedError on malformed input.

    Per Phase 2.1 C2 Option B consolidation (ratified 2026-05-11): malformed
    archives (``BadZipFile``, ``BadGzipFile``) raise
    :class:`SecurityRejectedError` з ``ErrorCategory.ARCHIVE_VIOLATION``,
    mirroring :func:`extract_archive_safely` canonical precedent. No separate
    ``ArchiveExtractionError`` exception class — single canonical exception
    hierarchy per KD-2.1-I spirit.
    """

    @pytest.mark.asyncio
    async def test_malformed_zip_raises_archive_violation(self, tmp_path: Path) -> None:
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"not actually a zip file")

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(bad_zip)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "zip" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_malformed_gz_raises_archive_violation(self, tmp_path: Path) -> None:
        bad_gz = tmp_path / "bad.gz"
        bad_gz.write_bytes(b"not actually gzipped")

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(bad_gz)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION
        assert "gzip" in exc_info.value.detail.lower()


# ── Phase 2.1 C3: ported coverage scenarios from legacy ────────────
#
# 6 scenarios migrated from tests/unit/test_safety_archive.py (legacy)
# during Phase 2.1 C3 commit per Option B ratify (2026-05-11). They
# preserve coverage for: ARCHIVE_BOMB defenses (max_files / oversized
# uncompressed / oversized gz), encoding fallback (non-UTF-8 → U+FFFD),
# zip-internal symlink SecurityWarning collection, and direct unit
# tests for the _submission_sanitize_filename helper.

import stat  # noqa: E402

from course_supporter.security.archive import (  # noqa: E402
    _submission_sanitize_filename,
)


class TestExtractSubmissionContentEncodingFallback:
    """Non-UTF-8 file bytes decode з U+FFFD replacement."""

    @pytest.mark.asyncio
    async def test_read_non_utf8_file_uses_replacement(self, tmp_path: Path) -> None:
        file_path = tmp_path / "notes.txt"
        file_path.write_bytes("caf\xe9 r\xe9sum\xe9".encode("latin-1"))

        result = await extract_submission_content(file_path)
        assert "caf" in result.files[0].content
        assert "�" in result.files[0].content


class TestExtractSubmissionContentArchiveBombCaps:
    """Settings-driven cap enforcement (max_files / max uncompressed size)."""

    @pytest.mark.asyncio
    async def test_too_many_files_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.security import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 50
            safety_archive_max_files = 2
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        archive_path = tmp_path / "many.zip"
        _write_zip_to_path(
            archive_path,
            [(f"file{i}.py", f"x = {i}\n".encode()) for i in range(5)],
        )

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(archive_path)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_BOMB
        assert "files" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_oversized_uncompressed_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.security import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 1
            safety_archive_max_files = 1000
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        archive_path = tmp_path / "big.zip"
        _write_zip_to_path(archive_path, [("big.py", b"x" * (2 * 1024 * 1024))])

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(archive_path)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_BOMB
        assert "exceeds limit" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_oversized_gz_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.security import archive

        class MockSettings:
            safety_archive_max_uncompressed_mb = 1
            safety_archive_max_files = 1000
            safety_archive_max_nesting = 1

        monkeypatch.setattr(archive, "get_settings", lambda: MockSettings())

        gz_path = tmp_path / "big.txt.gz"
        _write_gz_to_path(gz_path, b"x" * (2 * 1024 * 1024))

        with pytest.raises(SecurityRejectedError) as exc_info:
            await extract_submission_content(gz_path)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_BOMB
        assert "exceeds limit" in exc_info.value.detail.lower()


class TestExtractSubmissionContentZipSymlinkWarning:
    """Symlink entries inside .zip are skipped and yield SecurityWarning."""

    @pytest.mark.asyncio
    async def test_zip_symlink_entries_skipped_with_warning(
        self, tmp_path: Path
    ) -> None:
        archive_path = tmp_path / "with_symlink.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("real.py", b"x = 1\n")
            symlink_info = zipfile.ZipInfo("evil.py")
            symlink_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(symlink_info, "/etc/passwd")

        result = await extract_submission_content(archive_path)
        assert len(result.files) == 1
        assert result.files[0].filename == "real.py"
        symlink_warnings = [
            w for w in result.security_warnings if w.violation_type == "symlink"
        ]
        assert len(symlink_warnings) == 1
        assert symlink_warnings[0].raw_filename == "evil.py"


class TestSubmissionSanitizeFilename:
    """Direct unit tests for _submission_sanitize_filename helper."""

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
    def test_sanitize_returns_safe_path(self, raw: str, expected: str) -> None:
        assert _submission_sanitize_filename(raw) == expected

    @pytest.mark.parametrize("raw", ["../", "..", "/../.."])
    def test_sanitize_returns_none_for_unsafe(self, raw: str) -> None:
        assert _submission_sanitize_filename(raw) is None
