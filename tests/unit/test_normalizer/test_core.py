"""Tests for normalize_archive -- the deterministic orchestrator (KD18 P1).

Fixture archives are generated synthetically via stdlib zipfile / tarfile.
Covers the full pipeline: clean project, denylist collapse, binary
inclusion, magic-mismatch / nested exclusion, duplicate canonical path,
level-2 cap, empty archive, zip<->tar.gz parity, and mislabeled kind.
"""

import hashlib
import io
import tarfile
import zipfile

import pytest

from course_supporter.normalizer.core import normalize_archive
from course_supporter.normalizer.exceptions import (
    NormalizerError,
    NormalizerLimitError,
)
from course_supporter.normalizer.hashing import compute_aggregate_hash
from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedReason,
    NormalizerLimits,
)
from course_supporter.security.exceptions import SecurityRejectedError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(64)
ELF_BYTES = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 80


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def _tar_gz(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class TestCleanProject:
    def test_included_hash_and_zip_roundtrip(self) -> None:
        entries = [
            ("src/main.py", b"print('hello')\n"),
            ("README.md", b"# Readme\n"),
            ("db/schema.sql", b"SELECT 1;\n"),  # 4.5 fix: sql -> INCLUDED text
        ]
        snap = normalize_archive(_zip(entries), archive_kind="zip")

        assert {e.path for e in snap.manifest.included} == {
            "src/main.py",
            "README.md",
            "db/schema.sql",
        }
        assert snap.manifest.excluded == ()

        # snapshot_hash == aggregate_hash == recomputed over included.
        assert snap.snapshot_hash == snap.manifest.aggregate_hash
        assert snap.snapshot_hash == compute_aggregate_hash(snap.manifest.included)

        # canonical_zip round-trips to exactly the input files.
        with zipfile.ZipFile(io.BytesIO(snap.canonical_zip)) as zf:
            recovered = {n: zf.read(n) for n in zf.namelist()}
        assert recovered == dict(entries)

        classes = {e.path: e.cls for e in snap.manifest.included}
        assert classes["src/main.py"] is EntryClass.TEXT
        assert classes["db/schema.sql"] is EntryClass.TEXT

    def test_totals(self) -> None:
        entries = [("a.py", b"xx"), ("b.md", b"yyy")]
        snap = normalize_archive(_zip(entries), archive_kind="zip")
        assert snap.manifest.total_files == 2
        assert snap.manifest.total_bytes == 5


class TestDenylist:
    def test_dir_collapsed_survivors_intact(self) -> None:
        entries = [
            ("src/main.py", b"print(1)\n"),
            (".venv/lib/a.py", b"x = 1\n"),
            (".venv/lib/b.py", b"y = 2\n"),
        ]
        snap = normalize_archive(_zip(entries), archive_kind="zip")

        assert {e.path for e in snap.manifest.included} == {"src/main.py"}
        assert len(snap.manifest.excluded) == 1
        row = snap.manifest.excluded[0]
        assert row.path == ".venv/"
        assert row.reason is ExcludedReason.DENYLIST_DIR
        assert row.entries == 2
        # denylist bytes are counted in totals but excluded from the zip.
        with zipfile.ZipFile(io.BytesIO(snap.canonical_zip)) as zf:
            assert zf.namelist() == ["src/main.py"]

    def test_denylist_junk_is_hash_neutral(self) -> None:
        # №14 skip-before-accounting lock: adding denylist junk to an
        # archive changes NOTHING in the snapshot_hash (excluded rows
        # never participate in the aggregate hash).
        clean = [("src/main.py", b"print(1)\n"), ("README.md", b"# T\n")]
        junk = [
            ("node_modules/lodash/index.js", b"module.exports = {}\n"),
            ("__MACOSX/src/._main.py", b"\x00\x05\x16\x07" + b"\x00" * 60),
        ]
        clean_snap = normalize_archive(_zip(clean), archive_kind="zip")
        dirty_snap = normalize_archive(_zip(clean + junk), archive_kind="zip")
        assert dirty_snap.snapshot_hash == clean_snap.snapshot_hash
        assert {e.path for e in dirty_snap.manifest.included} == {
            "README.md",
            "src/main.py",
        }
        reasons = {e.path: e.reason for e in dirty_snap.manifest.excluded}
        assert reasons == {
            "node_modules/": ExcludedReason.DENYLIST_DIR,
            "__MACOSX/": ExcludedReason.DENYLIST_DIR,
        }

    def test_denylist_junk_exempt_from_unpack_guards(self) -> None:
        # №14: junk under a denylist prefix is never unpacked, so it
        # cannot trip the level-1 accounting guards it used to trip
        # (here: directory depth and the declared-total budget).
        deep = "node_modules/" + "/".join("abcdefghijklmnopqr") + "/x.js"
        big = b"\x00" * (2 * 1024 * 1024)
        limits = NormalizerLimits(raw_max_unzipped_bytes=1024 * 1024)
        snap = normalize_archive(
            _zip(
                [
                    ("src/main.py", b"print(1)\n"),
                    (deep, b"deep\n"),
                    ("node_modules/blob.js", big),
                ]
            ),
            archive_kind="zip",
            limits=limits,
        )
        assert {e.path for e in snap.manifest.included} == {"src/main.py"}
        row = snap.manifest.excluded[0]
        assert row.path == "node_modules/"
        assert row.entries == 2
        # Sizes come from the declared header (entries never read).
        assert row.size == len(big) + len(b"deep\n")


class TestVerdictMapping:
    def test_non_whitelisted_ext_included_as_binary(self) -> None:
        snap = normalize_archive(
            _zip([("logo.png", PNG_BYTES), ("main.py", b"print(1)\n")]),
            archive_kind="zip",
        )
        classes = {e.path: e.cls for e in snap.manifest.included}
        assert classes["logo.png"] is EntryClass.BINARY
        assert classes["main.py"] is EntryClass.TEXT

    def test_magic_mismatch_excluded(self) -> None:
        snap = normalize_archive(
            _zip([("evil.py", ELF_BYTES), ("ok.py", b"print(1)\n")]),
            archive_kind="zip",
        )
        assert {e.path for e in snap.manifest.included} == {"ok.py"}
        assert len(snap.manifest.excluded) == 1
        row = snap.manifest.excluded[0]
        assert row.path == "evil.py"
        assert row.reason is ExcludedReason.MAGIC_MISMATCH
        assert row.entries == 1

    def test_nested_archive_excluded_raw_hash(self) -> None:
        inner = _zip([("leaf.py", b"print(1)\n")])
        snap = normalize_archive(
            _zip([("bundle.zip", inner), ("main.py", b"print(2)\n")]),
            archive_kind="zip",
        )
        assert {e.path for e in snap.manifest.included} == {"main.py"}
        excluded = {e.path: e for e in snap.manifest.excluded}
        assert excluded["bundle.zip"].reason is ExcludedReason.NESTED_ARCHIVE
        # raw archive bytes, never unpacked.
        assert excluded["bundle.zip"].size == len(inner)


class TestDegenerateAndLimits:
    def test_duplicate_canonical_path_raises(self) -> None:
        # "./a.py" and "a.py" both canonicalize to "a.py".
        archive = _zip([("./a.py", b"print(1)\n"), ("a.py", b"print(2)\n")])
        with pytest.raises(NormalizerError):
            normalize_archive(archive, archive_kind="zip")

    def test_level2_kept_total_over_cap_raises(self) -> None:
        entries = [("a.txt", b"x" * 400), ("b.txt", b"y" * 400)]
        limits = NormalizerLimits(kept_total_max_bytes=500)
        with pytest.raises(NormalizerLimitError):
            normalize_archive(_zip(entries), archive_kind="zip", limits=limits)

    def test_single_file_over_cap_stays_included(self) -> None:
        # kept_single_max is a P4 threshold, NOT a normalizer filter.
        limits = NormalizerLimits(kept_single_max_bytes=10)
        snap = normalize_archive(
            _zip([("big.txt", b"x" * 100)]), archive_kind="zip", limits=limits
        )
        assert {e.path for e in snap.manifest.included} == {"big.txt"}
        assert snap.manifest.included[0].size == 100


class TestEdges:
    def test_empty_archive(self) -> None:
        snap = normalize_archive(_zip([]), archive_kind="zip")
        assert snap.manifest.included == ()
        assert snap.manifest.excluded == ()
        assert snap.manifest.total_files == 0
        assert snap.manifest.total_bytes == 0
        assert snap.snapshot_hash == hashlib.sha256(b"").hexdigest()

    def test_tar_gz_same_files_same_snapshot_hash(self) -> None:
        entries = [("src/main.py", b"print(1)\n"), ("README.md", b"# hi\n")]
        z = normalize_archive(_zip(entries), archive_kind="zip")
        t = normalize_archive(_tar_gz(entries), archive_kind="tar.gz")
        # aggregate_hash is over (path, raw-hash) -> container-independent.
        assert z.snapshot_hash == t.snapshot_hash

    def test_mislabeled_kind_raises_security_error(self) -> None:
        # zip bytes parsed as tar.gz -> structural raise, not translated.
        zip_bytes = _zip([("main.py", b"print(1)\n")])
        with pytest.raises(SecurityRejectedError):
            normalize_archive(zip_bytes, archive_kind="tar.gz")
