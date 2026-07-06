"""Tests for the additive classify mode of ``extract_archive_safely``.

KD18 P1 KD-A: the normalizer needs the full archive tree (including
non-whitelisted entries) to build a manifest that covers excluded
content, rather than aborting on the first violation. The classify
mode demotes the three content-level signals -- non-whitelisted
extension, magic mismatch, nested archive -- to annotated
:class:`ClassifiedEntry` yields, while the structural unpack-guards
(traversal, bomb, symlink, declared-size) still raise in both modes.

This wave proves two invariants:

* Strict path (``classify=False``, the production default) is
  byte-identical: nested archives recurse, non-whitelisted extensions
  and magic mismatches raise, valid entries yield ``ExtractedFile``.
* Classify path converts exactly the three content signals to yields,
  keeps the structural guards raising, never opens nested archives,
  and surfaces the whole tree without aborting on a bad entry.

Fixtures are generated synthetically via stdlib zipfile / tarfile at
call time -- no on-disk corpus (mirrors ``test_archive.py``).
"""

import io
import stat
import tarfile
import zipfile
from typing import Literal

import pytest

from course_supporter.security.archive import (
    ClassifiedEntry,
    EntryVerdict,
    ExtractedFile,
    extract_archive_safely,
)
from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)

# ── Fixture generators ─────────────────────────────────────────────


def _zip_fixture(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
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


def _tar_gz_symlink(name: str, *, linkname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = linkname
        info.size = 0
        tf.addfile(info)
    return buf.getvalue()


def _zip_with_symlink(real_name: str, link_name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(real_name, b"x = 1\n")
        link_info = zipfile.ZipInfo(link_name)
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link_info, target)
    return buf.getvalue()


# Real magic byte fixtures for the content gate.
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"trailer body\n" * 5
PE_BYTES = b"\x4d\x5a\x90\x00\x03\x00\x00\x00" + b"\x00" * 200
TEXT_BYTES = b"hello world\n"
BINARY_BYTES = bytes(range(256))

_WHITELIST = frozenset({"txt", "pdf", "zip"})
_BUDGET = 1 * 1024 * 1024  # 1 MB


def _classify(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"] = "zip",
    max_unzipped_size: int = _BUDGET,
    max_nesting_depth: int = 1,
    allowed_extensions: frozenset[str] = _WHITELIST,
) -> list[ClassifiedEntry]:
    return list(
        extract_archive_safely(
            archive_bytes,
            archive_kind=archive_kind,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
            classify=True,
        )
    )


def _strict(
    archive_bytes: bytes,
    *,
    archive_kind: Literal["zip", "tar.gz"] = "zip",
    max_unzipped_size: int = _BUDGET,
    max_nesting_depth: int = 3,
    allowed_extensions: frozenset[str] = _WHITELIST,
) -> list[ExtractedFile]:
    return list(
        extract_archive_safely(
            archive_bytes,
            archive_kind=archive_kind,
            max_unzipped_size=max_unzipped_size,
            max_nesting_depth=max_nesting_depth,
            allowed_extensions=allowed_extensions,
        )
    )


def _by_name(entries: list[ClassifiedEntry]) -> dict[str, ClassifiedEntry]:
    return {e.arcname: e for e in entries}


# ── Classify: the three content signals become verdicts ────────────


class TestClassifyConversions:
    def test_included_verdict_for_valid_entry(self) -> None:
        archive = _zip_fixture([("readme.txt", TEXT_BYTES)])
        entries = _classify(archive)
        assert len(entries) == 1
        assert isinstance(entries[0], ClassifiedEntry)
        assert entries[0].verdict is EntryVerdict.INCLUDED
        assert entries[0].content == TEXT_BYTES

    def test_included_verdict_for_document_bytes(self) -> None:
        archive = _zip_fixture([("paper.pdf", PDF_BYTES)])
        entries = _classify(archive)
        assert entries[0].verdict is EntryVerdict.INCLUDED

    def test_forbidden_type_becomes_yield_not_raise(self) -> None:
        # "data.bin" -- extension not in the whitelist.
        archive = _zip_fixture([("data.bin", BINARY_BYTES)])
        entries = _classify(archive)
        assert len(entries) == 1
        assert entries[0].verdict is EntryVerdict.FORBIDDEN_TYPE
        # Raw bytes preserved so the caller can hash the excluded entry.
        assert entries[0].content == BINARY_BYTES

    def test_no_extension_becomes_forbidden_type(self) -> None:
        archive = _zip_fixture([("Makefile", TEXT_BYTES)])
        entries = _classify(archive)
        assert entries[0].verdict is EntryVerdict.FORBIDDEN_TYPE

    def test_magic_mismatch_becomes_yield_not_raise(self) -> None:
        # arcname claims .pdf but the content is a PE binary.
        archive = _zip_fixture([("evil.pdf", PE_BYTES)])
        entries = _classify(archive)
        assert len(entries) == 1
        assert entries[0].verdict is EntryVerdict.MAGIC_MISMATCH
        assert entries[0].content == PE_BYTES

    def test_nested_archive_becomes_yield_not_opened(self) -> None:
        inner = _zip_fixture([("leaf.txt", TEXT_BYTES)])
        outer = _zip_fixture([("inner.zip", inner)])
        entries = _classify(outer)
        assert len(entries) == 1
        assert entries[0].verdict is EntryVerdict.NESTED_ARCHIVE
        # The nested archive is NOT opened -- content is its raw bytes,
        # and "leaf.txt" never appears.
        assert entries[0].content == inner
        assert entries[0].arcname == "inner.zip"

    def test_full_tree_seen_without_abort(self) -> None:
        # A mix of valid, forbidden, mismatch, and nested entries: the
        # normalizer-enabling property is that ALL of them surface.
        inner = _zip_fixture([("leaf.txt", TEXT_BYTES)])
        archive = _zip_fixture(
            [
                ("readme.txt", TEXT_BYTES),
                ("data.bin", BINARY_BYTES),
                ("evil.pdf", PE_BYTES),
                ("inner.zip", inner),
            ]
        )
        entries = _classify(archive)
        verdicts = {e.arcname: e.verdict for e in entries}
        assert verdicts == {
            "readme.txt": EntryVerdict.INCLUDED,
            "data.bin": EntryVerdict.FORBIDDEN_TYPE,
            "evil.pdf": EntryVerdict.MAGIC_MISMATCH,
            "inner.zip": EntryVerdict.NESTED_ARCHIVE,
        }

    def test_tar_gz_classify_parity(self) -> None:
        archive = _tar_gz_fixture(
            [
                ("readme.txt", TEXT_BYTES),
                ("data.bin", BINARY_BYTES),
            ]
        )
        entries = _by_name(_classify(archive, archive_kind="tar.gz"))
        assert entries["readme.txt"].verdict is EntryVerdict.INCLUDED
        assert entries["data.bin"].verdict is EntryVerdict.FORBIDDEN_TYPE

    def test_empty_archive_yields_nothing(self) -> None:
        assert _classify(_zip_fixture([])) == []


# ── Classify: structural guards still raise (level-1 unpack-guard) ──


class TestClassifyStructuralGuardsStillRaise:
    def test_path_traversal_still_raises(self) -> None:
        archive = _zip_fixture([("../etc/passwd", b"haha")])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(archive)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_per_entry_cap_still_raises(self) -> None:
        # cap == max_unzipped_size // 2 == 512.
        archive = _zip_fixture([("big.txt", b"x" * 600)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(archive, max_unzipped_size=1024)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_declared_total_still_raises(self) -> None:
        # Three 400-byte entries (each < 500 cap) sum to 1200 > 1000.
        archive = _zip_fixture(
            [
                ("a.txt", b"x" * 400),
                ("b.txt", b"x" * 400),
                ("c.txt", b"x" * 400),
            ]
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(archive, max_unzipped_size=1000)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_tar_symlink_still_raises(self) -> None:
        archive = _tar_gz_symlink("link", linkname="/etc/passwd")
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(archive, archive_kind="tar.gz")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_zip_symlink_still_raises(self) -> None:
        archive = _zip_with_symlink("real.txt", "link", "/etc/passwd")
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(archive)
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION

    def test_malformed_zip_still_raises(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            _classify(b"not a zip at all")
        assert exc_info.value.category is ErrorCategory.ARCHIVE_VIOLATION


# ── Strict path unchanged (byte-identical default) ─────────────────


class TestStrictPathUnchanged:
    def test_default_is_strict_and_yields_extractedfile(self) -> None:
        archive = _zip_fixture([("readme.txt", TEXT_BYTES)])
        entries = _strict(archive)
        assert len(entries) == 1
        assert type(entries[0]) is ExtractedFile
        assert entries[0].content == TEXT_BYTES

    def test_forbidden_extension_still_raises_in_strict(self) -> None:
        archive = _zip_fixture([("data.bin", BINARY_BYTES)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _strict(archive)
        assert exc_info.value.category is ErrorCategory.FORBIDDEN_TYPE

    def test_magic_mismatch_still_raises_in_strict(self) -> None:
        archive = _zip_fixture([("evil.pdf", PE_BYTES)])
        with pytest.raises(SecurityRejectedError) as exc_info:
            _strict(archive)
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    def test_nested_archive_recurses_in_strict(self) -> None:
        inner = _zip_fixture([("leaf.txt", TEXT_BYTES)])
        outer = _zip_fixture([("inner.zip", inner)])
        entries = _strict(outer)
        # Strict mode opens the nested archive: the leaf surfaces, and
        # no ClassifiedEntry is ever produced.
        assert len(entries) == 1
        assert type(entries[0]) is ExtractedFile
        assert entries[0].arcname == "leaf.txt"
        assert entries[0].content == TEXT_BYTES
        assert entries[0].depth == 1
