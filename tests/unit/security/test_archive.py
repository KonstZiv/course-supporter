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
* Denylist skip (№14): hostility rejects even inside the skip zone;
  skipped entries are exempt from all resource accounting
"""

import io
import tarfile
import zipfile

import pytest

from course_supporter.security.archive import (
    EntryVerdict,
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
        # ``archive_kind: str`` deliberately admits invalid kinds (e.g.
        # "rar") for the negative test; the classify overloads narrow it
        # to a Literal, so suppress the resulting call-overload here.
        extract_archive_safely(  # type: ignore[call-overload]
            archive_bytes,
            archive_kind=archive_kind,
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
        # depth = 16 (sixteen slashes after rstrip) — №14 raised from 8.
        deep = "/".join("abcdefghijklmnop") + "/leaf.txt"
        archive = _zip_fixture([(deep, b"hi")])
        files = _extract(archive)
        assert files[0].arcname == deep

    def test_directory_depth_exceeded_rejected(self) -> None:
        # depth = 17 → reject.
        too_deep = "/".join("abcdefghijklmnopq") + "/leaf.txt"
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


# ── Denylist skip (№14 jurisdiction split) ─────────────────────────


def _junk_matcher(arcname: str) -> str | None:
    """Minimal skip matcher: everything under top-level ``junk/``."""
    return "junk/" if arcname.startswith("junk/") else None


class TestDenylistSkip:
    def test_classify_yields_denylist_skip_with_declared_size(self) -> None:
        archive = _zip_fixture([("junk/lib.txt", b"x" * 64), ("kept.txt", b"hello")])
        entries = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                classify=True,
                skip_matcher=_junk_matcher,
            )
        )
        by_name = {e.arcname: e for e in entries}
        skipped = by_name["junk/lib.txt"]
        assert skipped.verdict is EntryVerdict.DENYLIST_SKIP
        assert skipped.content == b""
        assert skipped.declared_size == 64
        kept = by_name["kept.txt"]
        assert kept.verdict is EntryVerdict.INCLUDED
        assert kept.declared_size == len(b"hello")

    def test_strict_mode_silently_drops_skip_zone(self) -> None:
        # junk/evil.exe would raise FORBIDDEN_TYPE without the matcher.
        archive = _zip_fixture([("junk/evil.exe", PE_BYTES), ("kept.txt", b"hello")])
        with pytest.raises(SecurityRejectedError):
            _extract(archive)
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_skip_zone_exempt_from_declared_total_budget(self) -> None:
        # Without the matcher the declared-total pre-check rejects.
        big = b"\x00" * (2 * _DEFAULT_BUDGET)
        archive = _zip_fixture([("junk/blob.txt", big), ("kept.txt", b"hi")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _extract(archive)
        assert "declared total" in exc_info.value.detail
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_skip_zone_exempt_from_per_entry_cap_and_ratio(self) -> None:
        # 600 KB of zeros: over the per-entry cap (budget // 2 = 512 KB)
        # AND >100x deflate ratio — both accounting guards. A skipped
        # entry is never read, so neither fires.
        big = b"\x00" * (600 * 1024)
        archive = _zip_fixture(
            [("junk/zeros.txt", big), ("kept.txt", b"hi")],
            compression=zipfile.ZIP_DEFLATED,
        )
        with pytest.raises(SecurityRejectedError):
            _extract(archive)
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_skip_zone_exempt_from_directory_depth(self) -> None:
        deep = "junk/" + "/".join("abcdefghijklmnopqr") + "/leaf.txt"
        archive = _zip_fixture([(deep, b"x"), ("kept.txt", b"hi")])
        with pytest.raises(SecurityRejectedError):
            _extract(archive)
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_depth_still_enforced_outside_skip_zone(self) -> None:
        too_deep = "/".join("abcdefghijklmnopq") + "/leaf.txt"
        archive = _zip_fixture([(too_deep, b"x")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            list(
                extract_archive_safely(
                    archive,
                    archive_kind="zip",
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    skip_matcher=_junk_matcher,
                )
            )
        assert "directory depth" in exc_info.value.detail.lower()

    def test_deep_directory_entry_still_checked_outside_skip(self) -> None:
        # A pure directory entry (trailing slash) beyond the limit
        # rejects outside the skip zone, passes inside it.
        deep_dir = "/".join("abcdefghijklmnopqr") + "/"
        rejected = _zip_fixture([(deep_dir, b""), ("kept.txt", b"hi")])
        with pytest.raises(SecurityRejectedError):
            list(
                extract_archive_safely(
                    rejected,
                    archive_kind="zip",
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    skip_matcher=_junk_matcher,
                )
            )
        accepted = _zip_fixture([("junk/" + deep_dir, b""), ("kept.txt", b"hi")])
        files = list(
            extract_archive_safely(
                accepted,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_hostile_traversal_inside_skip_zone_rejects(self) -> None:
        archive = _zip_fixture([("junk/../../etc/passwd", b"x")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            list(
                extract_archive_safely(
                    archive,
                    archive_kind="zip",
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    skip_matcher=_junk_matcher,
                )
            )
        assert "traversal" in exc_info.value.detail.lower()

    def test_zip_symlink_inside_skip_zone_rejects(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("junk/evil-link")
            info.external_attr = 0o120777 << 16  # S_IFLNK | 0o777
            zf.writestr(info, b"target")
        with pytest.raises(SecurityRejectedError) as exc_info:
            list(
                extract_archive_safely(
                    buf.getvalue(),
                    archive_kind="zip",
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    skip_matcher=_junk_matcher,
                )
            )
        assert "non-regular" in exc_info.value.detail.lower()

    def test_tar_special_member_inside_skip_zone_rejects(self) -> None:
        archive = _tar_gz_with_special_member("junk/dev", tarfile.CHRTYPE)
        with pytest.raises(SecurityRejectedError) as exc_info:
            list(
                extract_archive_safely(
                    archive,
                    archive_kind="tar.gz",
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    skip_matcher=_junk_matcher,
                )
            )
        assert "non-regular" in exc_info.value.detail.lower()

    def test_tar_skip_zone_yields_declared_size(self) -> None:
        archive = _tar_gz_fixture([("junk/lib.txt", b"z" * 32), ("kept.txt", b"ok")])
        entries = list(
            extract_archive_safely(
                archive,
                archive_kind="tar.gz",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                classify=True,
                skip_matcher=_junk_matcher,
            )
        )
        by_name = {e.arcname: e for e in entries}
        assert by_name["junk/lib.txt"].verdict is EntryVerdict.DENYLIST_SKIP
        assert by_name["junk/lib.txt"].declared_size == 32
        assert by_name["kept.txt"].verdict is EntryVerdict.INCLUDED

    def test_nested_archive_inside_skip_zone_not_opened(self) -> None:
        # Strict mode without the matcher recurses into junk/inner.zip
        # and rejects the .exe inside; with the matcher the nested
        # archive is dropped before the recursion gate — never opened.
        inner = _zip_fixture([("malware.exe", PE_BYTES)])
        archive = _zip_fixture(
            [("junk/inner.zip", inner), ("kept.txt", b"kept text\n")]
        )
        with pytest.raises(SecurityRejectedError):
            _extract(archive)
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=_junk_matcher,
            )
        )
        assert [f.arcname for f in files] == ["kept.txt"]

    def test_canonical_denylist_prefix_matcher(self) -> None:
        # Integration lock: the production matcher (KD18 canonical
        # denylist) plugs straight into the kwarg.
        from course_supporter.normalizer.classify import denylist_prefix

        archive = _zip_fixture(
            [
                ("node_modules/pkg/index.txt", b"module text\n"),
                ("src.txt", b"source text\n"),
            ]
        )
        files = list(
            extract_archive_safely(
                archive,
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
                skip_matcher=denylist_prefix,
            )
        )
        assert [f.arcname for f in files] == ["src.txt"]


# ── Incident-form fixture (№14 regression precursor) ───────────────

# AppleDouble header bytes — what macOS Finder actually writes into
# __MACOSX/._* companion entries.
_APPLE_DOUBLE = bytes.fromhex("00051607") + b"\x00\x02\x00\x00" + b"\x00" * 60

# The framework-cache subtree of the 2026-07-12 prod incident: 8 levels
# deep on its own; the __MACOSX/ prefix pushed its mirror to depth 9.
_INCIDENT_CACHE = ".angular/cache/21.1.4/app/vite/deps_ssr/chunks"


def _incident_form_zip() -> bytes:
    """Synthetic mirror of the prod-incident archive shape (№14).

    A real lesson archive packed "as is" on macOS: honest sources at
    the top, Finder junk (``__MACOSX/`` AppleDouble companions) layered
    OVER a framework cache that is itself 8 levels deep — the deepest
    arcname reaches depth 9, which the pre-№14 depth-8 guard rejected
    wholesale.
    """
    return _zip_fixture(
        [
            ("app/src/main.txt", b"honest lesson source\n"),
            ("app/readme.pdf", PDF_BYTES),
            (f"app/{_INCIDENT_CACHE}/chunk-ABC.txt", b"generated vite chunk\n"),
            (f"__MACOSX/app/{_INCIDENT_CACHE}/._chunk-ABC.txt", _APPLE_DOUBLE),
            ("__MACOSX/app/._readme.pdf", _APPLE_DOUBLE),
            (".DS_Store", b"\x00\x01Bud1"),
        ]
    )


class TestIncidentFormDepth:
    """The prod-incident SHAPE passes the raised depth limit structurally."""

    def test_incident_form_passes_structurally_at_depth_16(self) -> None:
        entries = list(
            extract_archive_safely(
                _incident_form_zip(),
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                # Archive-RECURSION cap mirroring the production
                # authored envelope (max_archive_nesting_depth=1,
                # top-level only) — NOT the №14 directory-depth limit.
                max_nesting_depth=1,
                allowed_extensions=frozenset({"txt", "pdf"}),
                classify=True,
            )
        )
        by_name = {e.arcname: e.verdict for e in entries}
        # Honest content survives; nothing raises (pre-№14 this archive
        # was rejected wholesale on the __MACOSX depth-9 arcname).
        assert by_name["app/src/main.txt"] is EntryVerdict.INCLUDED
        assert by_name["app/readme.pdf"] is EntryVerdict.INCLUDED
        # Finder junk surfaces as content verdicts here; the canonical
        # matcher (next test) turns these into DENYLIST_SKIP.
        assert by_name["__MACOSX/app/._readme.pdf"] is EntryVerdict.MAGIC_MISMATCH
        assert by_name[".DS_Store"] is EntryVerdict.FORBIDDEN_TYPE

    def test_incident_form_junk_skipped_with_canonical_matcher(self) -> None:
        """№14 regress: the incident archive passes; junk is skipped."""
        from course_supporter.normalizer.classify import denylist_prefix

        entries = list(
            extract_archive_safely(
                _incident_form_zip(),
                archive_kind="zip",
                max_unzipped_size=_DEFAULT_BUDGET,
                # Archive-RECURSION cap mirroring the production
                # authored envelope (max_archive_nesting_depth=1,
                # top-level only) — NOT the №14 directory-depth limit.
                max_nesting_depth=1,
                allowed_extensions=frozenset({"txt", "pdf"}),
                classify=True,
                skip_matcher=denylist_prefix,
            )
        )
        verdicts = {e.arcname: e.verdict for e in entries}
        assert verdicts["app/src/main.txt"] is EntryVerdict.INCLUDED
        assert verdicts["app/readme.pdf"] is EntryVerdict.INCLUDED
        junk = {n for n, v in verdicts.items() if v is EntryVerdict.DENYLIST_SKIP}
        assert junk == {
            f"app/{_INCIDENT_CACHE}/chunk-ABC.txt",
            f"__MACOSX/app/{_INCIDENT_CACHE}/._chunk-ABC.txt",
            "__MACOSX/app/._readme.pdf",
            ".DS_Store",
        }
        # Nothing beyond the two honest files and the four junk rows.
        assert len(verdicts) == 6


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


# ── Entry-count ceiling (gates §1.5, DD-6-S) ───────────────────────


# Real text, not a single byte: the strict mode verifies each member against
# its extension, and libmagic cannot call one character "text/plain".
_TINY_TEXT = b"a short line of real text\n"


def _many_entry_zip(count: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(count):
            zf.writestr(f"f{i}.txt", _TINY_TEXT)
    return buf.getvalue()


def _many_entry_tar_gz(count: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(count):
            info = tarfile.TarInfo(f"f{i}.txt")
            info.size = len(_TINY_TEXT)
            tf.addfile(info, io.BytesIO(_TINY_TEXT))
    return buf.getvalue()


class TestEntryCountCeiling:
    """An archive of many tiny files passes the byte budget and costs anyway.

    The ceiling was the one check only the legacy submission extractor had;
    the canonical extractor bounded total bytes but not member count, so a
    hundred thousand empty files sailed through and were paid for downstream.
    Folded in here on the way to deleting that extractor (DD-6-S), applied to
    both kinds and both modes -- the annotated mode would otherwise turn the
    ceiling into a very long list of annotations instead of a refusal.
    """

    @pytest.fixture(autouse=True)
    def _tiny_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr(
            "course_supporter.security.archive.get_settings",
            lambda: SimpleNamespace(safety_archive_max_files=3),
        )

    @pytest.mark.parametrize("kind", ["zip", "tar.gz"])
    @pytest.mark.parametrize("classify", [False, True])
    def test_over_the_ceiling_is_refused(self, kind: str, classify: bool) -> None:
        archive = _many_entry_zip(4) if kind == "zip" else _many_entry_tar_gz(4)
        with pytest.raises(SecurityRejectedError) as exc_info:
            list(
                extract_archive_safely(  # type: ignore[call-overload]
                    archive,
                    archive_kind=kind,
                    max_unzipped_size=_DEFAULT_BUDGET,
                    max_nesting_depth=3,
                    allowed_extensions=_DEFAULT_WHITELIST,
                    classify=classify,
                )
            )
        assert exc_info.value.category is ErrorCategory.ARCHIVE_BOMB
        assert "4 files" in exc_info.value.detail

    @pytest.mark.parametrize("kind", ["zip", "tar.gz"])
    def test_at_the_ceiling_passes(self, kind: str) -> None:
        archive = _many_entry_zip(3) if kind == "zip" else _many_entry_tar_gz(3)
        entries = list(
            extract_archive_safely(  # type: ignore[call-overload]
                archive,
                archive_kind=kind,
                max_unzipped_size=_DEFAULT_BUDGET,
                max_nesting_depth=3,
                allowed_extensions=_DEFAULT_WHITELIST,
            )
        )
        assert len(entries) == 3
