"""Attempts: one fingerprint for the same work, a difference for another (task 13, K1).

The locks, each through the real Stage 1 and the real doors where the doors
are what reads the work:

* а. the same work in UTF-8, UTF-16 with its BOM, CP1251 and with CRLF gives
  ONE fingerprint — and so does an archive member with a BOM, CRLF or in NFD;
* б. another indentation of a line of code, or one more empty line, gives
  ANOTHER fingerprint;
* в. an archive with the same files in another zip order gives the same
  fingerprint;
* г. the block naming what was not opened is no part of the fingerprint: the
  same student files with an unread file beside them and without it are one;
* ґ. a project's fingerprint is the stored ``snapshot_hash``; a test is never
  deduplicated, and its fingerprint holds for the same answers in another key
  order;
* д. a text difference counts its lines right on a known example, and the same
  work has an empty one.
"""

from __future__ import annotations

import io
import json
import unicodedata
import uuid
import zipfile

import pytest

from course_supporter.homework.attempts import (
    AttemptKind,
    AttemptSnapshot,
    AttemptWork,
    ProjectAttemptSnapshotter,
    TestAttemptSnapshotter,
    TextAttemptSnapshotter,
    attempt_snapshotter_for,
)
from course_supporter.homework.doors import (
    DoorReading,
    assemble_submission_work,
    extract_document_text,
)
from course_supporter.homework.test_doors import stored_answers
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer import compute_delta, normalize_archive
from course_supporter.normalizer.classify import denylist_prefix
from course_supporter.security.stage1 import run_stage1

TEXT = TextAttemptSnapshotter()
PROJECT = ProjectAttemptSnapshotter()
TEST = TestAttemptSnapshotter()

# Ukrainian code and comments, long enough (> 240 bytes) for the doors to
# recover a CP1251 file rather than refuse it, and with the stop words their
# verification asks for.
WORK = (
    "# Розв'язок домашнього завдання: пошук дублікатів у списку.\n"
    "def find_duplicates(items):\n"
    '    """Повертає елементи, що трапляються більше одного разу."""\n'
    "    seen = set()\n"
    "    duplicates = []\n"
    "    for item in items:\n"
    "        if item in seen:\n"
    "            duplicates.append(item)  # вже бачили цей елемент\n"
    "        seen.add(item)\n"
    "    return duplicates\n"
)
HELPER = "def double(n):\n    return n * 2\n"
# One work as a student's editor may have saved it.
SAME_WORK = {
    "utf-8": WORK.encode(),
    "utf-8 with BOM": b"\xef\xbb\xbf" + WORK.encode(),
    "utf-16 with BOM": WORK.encode("utf-16"),
    "cp1251": WORK.encode("cp1251"),
    "utf-8, CRLF": WORK.replace("\n", "\r\n").encode(),
    "cp1251, CRLF": WORK.replace("\n", "\r\n").encode("cp1251"),
    "utf-16 with BOM, CRLF": WORK.replace("\n", "\r\n").encode("utf-16"),
}
SALES_CSV = b"date,region,amount\n" + b"2026-10-01,north,1250.50\n" * 20


def _zip(*members: tuple[str, bytes]) -> bytes:
    """An archive with ``members`` in exactly this order."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members:
            zf.writestr(name, content)
    return buf.getvalue()


def _read(raw: bytes, filename: str) -> DoorReading:
    """What the new path's doors read out of an upload (``path_runner``)."""
    result = run_stage1(
        filename=filename,
        content=raw,
        context="homework",
        languages=["ukr"],
        archive_skip_matcher=denylist_prefix,
        document_extractor=extract_document_text,
    )
    text, not_opened, files = assemble_submission_work(
        result, file_bytes=raw, filename=filename
    )
    return DoorReading(
        text=text,
        not_opened=not_opened,
        recovered_encoding=result.recovered_encoding,
        files=files,
    )


def _text_snapshot(raw: bytes, filename: str = "solution.py") -> AttemptSnapshot:
    return TEXT.snapshot(AttemptWork(reading=_read(raw, filename)))


def _fingerprint(raw: bytes, filename: str = "solution.py") -> str:
    return _text_snapshot(raw, filename).fingerprint


class TestLockAOneWorkInEveryEncoding:
    """а. What the encoding put there is not the work."""

    def test_a_single_file_has_one_fingerprint(self) -> None:
        readings = {name: _read(raw, "solution.py") for name, raw in SAME_WORK.items()}
        fingerprints = {
            TEXT.snapshot(AttemptWork(reading=reading)).fingerprint
            for reading in readings.values()
        }

        assert readings["utf-16 with BOM"].recovered_encoding == "utf_16"
        assert readings["cp1251"].recovered_encoding == "cp1251"
        # The doors hand on a BOM and CRLF as they found them, so it is the
        # normalization that makes these one work.
        assert len({r.safety_text for r in readings.values()}) > 1
        assert len(fingerprints) == 1

    def test_an_archive_member_has_one_fingerprint(self) -> None:
        nfd = unicodedata.normalize("NFD", WORK)
        assert nfd != WORK  # й and ї decompose: a zip packed on macOS

        variants = [
            _zip(("solution.py", WORK.encode()), ("helper.py", HELPER.encode())),
            _zip(
                ("solution.py", b"\xef\xbb\xbf" + WORK.replace("\n", "\r\n").encode()),
                ("helper.py", HELPER.encode()),
            ),
            _zip(
                ("solution.py", WORK.encode("cp1251")), ("helper.py", HELPER.encode())
            ),
            _zip(("solution.py", nfd.encode()), ("helper.py", HELPER.encode())),
        ]

        assert len({_fingerprint(raw, "hw.zip") for raw in variants}) == 1


class TestLockBWhitespaceOfCodeIsTheWork:
    """б. Indentation, spaces inside a line and empty lines are the student's."""

    BASE = WORK.encode()

    @pytest.mark.parametrize(
        "changed",
        [
            WORK.replace("        seen.add(item)", "            seen.add(item)"),
            WORK.replace("    seen = set()\n", "    seen = set()\n\n"),
            WORK.replace("seen = set()", "seen  = set()"),
        ],
        ids=["indentation", "one more empty line", "a space inside a line"],
    )
    def test_another_fingerprint(self, changed: str) -> None:
        assert changed != WORK
        assert _fingerprint(changed.encode()) != _fingerprint(self.BASE)


class TestLockCArchiveOrderIsNotTheWork:
    """в. The zip's order of the same files is how they were packed, not the work."""

    def test_the_same_files_in_another_order_are_one_work(self) -> None:
        members = [
            ("src/solution.py", WORK.encode()),
            ("README.md", b"# Notes\n--- src/solution.py ---\nnot a frame\n"),
            ("helper.py", HELPER.encode()),
        ]
        forward = _read(_zip(*members), "hw.zip")
        backward = _read(_zip(*reversed(members)), "hw.zip")

        # The doors lay the files out in the zip's order ...
        assert forward.text != backward.text
        # ... and the fingerprint does not follow it.
        assert (
            TEXT.snapshot(AttemptWork(reading=forward)).fingerprint
            == TEXT.snapshot(AttemptWork(reading=backward)).fingerprint
        )

    def test_another_file_name_is_another_work(self) -> None:
        one = _zip(("a.py", HELPER.encode()), ("b.py", WORK.encode()))
        other = _zip(("a.py", HELPER.encode()), ("c.py", WORK.encode()))

        assert _fingerprint(one, "hw.zip") != _fingerprint(other, "hw.zip")


class TestLockDNotOpenedIsNotTheWork:
    """г. The block of what was not opened is the platform's line."""

    # Since 2026-11-05 a .csv is read as text; the shape of 04.11 — a lesson file
    # the policy does not admit — is kept with .xls.
    def test_an_unread_file_beside_the_same_work_changes_nothing(self) -> None:
        with_unread = _read(
            _zip(
                ("solution.py", WORK.encode()),
                ("sales.xls", SALES_CSV),
                (".env", b"TOKEN=1\n"),
            ),
            "hw.zip",
        )
        without = _read(_zip(("solution.py", WORK.encode())), "hw.zip")

        assert len(with_unread.not_opened) == 2
        assert "NOT OPENED" in with_unread.text
        assert (
            TEXT.snapshot(AttemptWork(reading=with_unread)).fingerprint
            == TEXT.snapshot(AttemptWork(reading=without)).fingerprint
        )


def _project(*members: tuple[str, bytes]) -> AttemptWork:
    """A project attempt as the doors leave it: the stored snapshot beside."""
    snapshot = normalize_archive(_zip(*members), archive_kind="zip")
    return AttemptWork(
        reading=DoorReading(text="(the project's Mentor context)"),
        snapshot_hash=snapshot.snapshot_hash,
        manifest=snapshot.manifest,
    )


class TestLockGProjectAndTest:
    """ґ. The project's stored hash; the test's canonical record, never deduplicated."""

    def test_a_project_fingerprint_is_the_stored_snapshot_hash(self) -> None:
        work = _project(("src/app.py", WORK.encode()), ("README.md", b"# App\n"))

        snapshot = PROJECT.snapshot(work)

        assert snapshot.kind is AttemptKind.PROJECT
        assert snapshot.fingerprint == work.snapshot_hash
        assert PROJECT.dedup_enabled is True

    def test_a_project_without_its_stored_snapshot_is_refused(self) -> None:
        work = _project(("src/app.py", WORK.encode()))
        other = _project(("src/app.py", HELPER.encode()))

        with pytest.raises(ValueError, match="snapshot hash and manifest"):
            PROJECT.snapshot(AttemptWork(reading=work.reading))
        with pytest.raises(ValueError, match="not the snapshot"):
            PROJECT.snapshot(
                AttemptWork(
                    reading=work.reading,
                    snapshot_hash=work.snapshot_hash,
                    manifest=other.manifest,
                )
            )

    def test_a_test_is_never_deduplicated(self) -> None:
        assert TEST.dedup_enabled is False
        assert TEXT.dedup_enabled is True

    def test_a_test_fingerprint_holds_in_any_key_order(self) -> None:
        version = uuid.UUID(int=7)
        canonical = stored_answers(version, {"1": ["а"], "2": ["б", "в"]})
        reordered = json.dumps(
            {"version_id": str(version), "answers": {"2": ["в", "б"], "1": ["а"]}},
            ensure_ascii=False,
            indent=2,
        )

        def fingerprint(text: str) -> str:
            return TEST.snapshot(
                AttemptWork(reading=DoorReading(text=text))
            ).fingerprint

        assert reordered != canonical
        assert fingerprint(reordered) == fingerprint(canonical)
        assert fingerprint(stored_answers(version, {"1": ["б"], "2": ["б", "в"]})) != (
            fingerprint(canonical)
        )

    def test_a_test_difference_is_empty(self) -> None:
        version = uuid.UUID(int=7)
        one = TEST.snapshot(
            AttemptWork(reading=DoorReading(text=stored_answers(version, {"1": ["а"]})))
        )
        other = TEST.snapshot(
            AttemptWork(reading=DoorReading(text=stored_answers(version, {"1": ["б"]})))
        )

        assert TEST.diff(one, other).is_empty


class TestLockETextDifference:
    """д. The lines added and removed, right on a known example; none for one work."""

    def test_the_counts_on_a_known_example(self) -> None:
        before = _text_snapshot(b"alpha\nbeta\ngamma\n", "notes.txt")
        after = _text_snapshot(b"alpha\nBETA\ngamma\ndelta\n", "notes.txt")

        diff = TEXT.diff(before, after)

        assert (diff.lines_added, diff.lines_removed) == (2, 1)
        assert diff.unified_diff == (
            "--- previous\n"
            "+++ current\n"
            "@@ -1,3 +1,4 @@\n"
            " alpha\n"
            "-beta\n"
            "+BETA\n"
            " gamma\n"
            "+delta\n"
        )
        assert not diff.is_empty

    def test_the_same_work_has_an_empty_difference(self) -> None:
        lf = _text_snapshot(WORK.encode())
        crlf_cp1251 = _text_snapshot(WORK.replace("\n", "\r\n").encode("cp1251"))

        diff = TEXT.diff(lf, crlf_cp1251)

        assert diff.is_empty
        assert (diff.unified_diff, diff.lines_added, diff.lines_removed) == ("", 0, 0)

    def test_a_final_line_end_alone_is_a_visible_change(self) -> None:
        diff = TEXT.diff(
            _text_snapshot(b"x = 1\n", "a.py"), _text_snapshot(b"x = 1", "a.py")
        )

        assert (diff.lines_added, diff.lines_removed) == (1, 1)
        assert "\\ No newline at end of file\n" in diff.unified_diff

    def test_only_a_line_feed_ends_a_line(self) -> None:
        # The doors refuse a form feed today; the difference does not lean on
        # that: its lines are the LF lines the normalization defines.
        def snapshot(text: str) -> AttemptSnapshot:
            return TEXT.snapshot(AttemptWork(reading=DoorReading(text=text)))

        diff = TEXT.diff(
            snapshot("page one\x0cstill one\u2028and one\n"),
            snapshot("page one\x0cstill one\u2028and one\nnext\n"),
        )

        assert (diff.lines_added, diff.lines_removed) == (1, 0)


class TestProjectDifference:
    """The project's difference is the existing delta of the two manifests."""

    def test_the_file_counts_and_the_delta(self) -> None:
        same = b"b = 1\n"
        previous = _project(("a.py", b"a = 1\n"), ("b.py", same), ("c.py", b"c = 1\n"))
        current = _project(("a.py", b"a = 2\n"), ("b.py", same), ("d.py", b"d = 1\n"))
        assert previous.manifest is not None
        assert current.manifest is not None

        diff = PROJECT.diff(PROJECT.snapshot(previous), PROJECT.snapshot(current))

        assert (diff.files_changed, diff.files_added, diff.files_removed) == (1, 1, 1)
        assert diff.project_delta == compute_delta(previous.manifest, current.manifest)
        assert diff.project_delta.changed == ("a.py",)
        assert not diff.is_empty

    def test_the_same_project_has_an_empty_difference(self) -> None:
        one = PROJECT.snapshot(_project(("a.py", b"a = 1\n")))
        again = PROJECT.snapshot(_project(("a.py", b"a = 1\n")))

        assert PROJECT.diff(one, again).is_empty


class TestTheFactory:
    """One adapter per type, chosen in one place; kinds are not compared across."""

    @pytest.mark.parametrize(
        ("task_type", "adapter"),
        [
            (AssignmentType.TEST, TestAttemptSnapshotter),
            (AssignmentType.SHORT_TASK, TextAttemptSnapshotter),
            (AssignmentType.TASK, TextAttemptSnapshotter),
            (AssignmentType.PROJECT, ProjectAttemptSnapshotter),
        ],
    )
    def test_every_type_has_its_adapter(
        self, task_type: AssignmentType, adapter: type
    ) -> None:
        assert isinstance(attempt_snapshotter_for(task_type.value), adapter)

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="essay"):
            attempt_snapshotter_for("essay")

    def test_snapshots_of_another_kind_are_not_compared(self) -> None:
        text = _text_snapshot(WORK.encode())
        project = PROJECT.snapshot(_project(("a.py", b"a = 1\n")))

        with pytest.raises(ValueError, match="cannot compare"):
            TEXT.diff(project, text)
        with pytest.raises(ValueError, match="cannot compare"):
            PROJECT.diff(project, text)
        with pytest.raises(ValueError, match="cannot compare"):
            TEST.diff(text, text)
