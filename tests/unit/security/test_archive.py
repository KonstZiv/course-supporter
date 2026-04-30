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
