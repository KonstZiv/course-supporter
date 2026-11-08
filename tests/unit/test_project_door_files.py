"""The project's door hands the evaluation the student's files (project chain K1).

A project is the author's base with the student's work on top. What the
evaluation of criteria may quote is the student's part: each file new or
changed against the base, as the screen read it (``DoorReading.files``), and
beside each changed file the base version it was changed from
(``DoorReading.base_files``), so that a later step can tell a quote of the
student's from one of the author's. The text the models read and the text the
safety check reads stay exactly what they were.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from course_supporter.homework import project_submission as mod
from course_supporter.homework.doors import DoorReading, not_opened_block
from course_supporter.homework.mentor_context import Side, build_mentor_context
from course_supporter.homework.project_submission import process_project_submission
from course_supporter.normalizer import (
    _PROJECT_NORMALIZE_LIMITS,
    DefaultTextExtractor,
    Delta,
    ManifestEntry,
    compute_delta,
    manifest_to_jsonb,
    normalize_archive,
)
from course_supporter.normalizer.models import EntryClass, ExcludedReason
from course_supporter.security.exceptions import ErrorCategory
from tests.unit.test_project_one_door import _docx
from tests.unit.test_project_submission import (
    _FakeBase,
    _FakeHwRepo,
    _FakeRepo,
    _FakeS3,
    _FakeSession,
    _FakeSubmission,
    _zip_bytes,
)

_LANGUAGES = ("ukr",)
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

BASE: dict[str, bytes] = {
    "app.py": b"def solve(n):\n    return n\n",
    "util.py": b"def helper():\n    return 1\n",
    "old.py": b"gone = True\n",
    "logo.png": _PNG,
}
# Every class a file of a project can be in, against BASE.
PROJECT: dict[str, bytes] = {
    # changed -- and it names util.py, so the context shows that one too
    "app.py": b"# uses util.py\ndef solve(n):\n    return n * 2\n",
    "util.py": BASE["util.py"],  # unchanged
    "lib/extra.py": b"def extra():\n    return 3\n",  # new
    ".env": b"OPENAI_API_KEY=sk-live-secret\n",  # a secret
    "logo.png": _PNG[:-1] + b"\x01",  # binary, changed
    "assets/icon.png": _PNG,  # binary, new
    "notes.txt": "Розв'язок\n".encode("cp1251"),  # the screen cannot read it
    # old.py: deleted
}


def _pdf(text: str) -> bytes:
    import fitz

    document = fitz.open()
    document.new_page().insert_text((72, 72), text)
    return bytes(document.tobytes())


async def _door(
    monkeypatch: pytest.MonkeyPatch,
    project: dict[str, bytes],
    base: dict[str, bytes] | None = None,
) -> tuple[DoorReading, _FakeS3]:
    """The project's door, with ``base`` attached as its stored snapshot."""
    monkeypatch.setattr(mod, "project_context_budget_chars", lambda: 1_000_000)
    s3 = _FakeS3()
    base_id = None
    if base is not None:
        snapshot = normalize_archive(_zip_bytes(base), archive_kind="zip")
        stored = _FakeBase(
            version=1,
            snapshot_key="base/k",
            manifest=manifest_to_jsonb(snapshot.manifest),
        )
        monkeypatch.setattr(
            mod, "ProjectBaseRepository", lambda session: _FakeRepo(stored, stored)
        )
        s3 = _FakeS3(base_snapshot_zip=snapshot.canonical_zip)
        base_id = uuid.uuid4()
    reading = await process_project_submission(
        session=_FakeSession(),  # type: ignore[arg-type]
        s3=s3,  # type: ignore[arg-type]
        hw_repo=_FakeHwRepo(),  # type: ignore[arg-type]
        submission=_FakeSubmission(base_id=base_id, authored_document_id=uuid.uuid4()),  # type: ignore[arg-type]
        sid=uuid.uuid4(),
        jid=uuid.uuid4(),
        file_bytes=_zip_bytes(project),
        raw_key="homework/t/s/proj.zip",
        languages=_LANGUAGES,
    )
    assert reading is not None
    return reading, s3


def _delta(project: dict[str, bytes], base: dict[str, bytes]) -> Delta:
    sub = normalize_archive(
        _zip_bytes(project), archive_kind="zip", limits=_PROJECT_NORMALIZE_LIMITS
    )
    return compute_delta(
        normalize_archive(_zip_bytes(base), archive_kind="zip").manifest,
        sub.manifest,
    )


def _context_as_before(project: dict[str, bytes], base: dict[str, bytes]) -> str:
    """The text the door gave before K1, from the parts it is made of.

    The snapshot, the screen's texts, the base read by the extractor, the
    delta context over them, and the block naming what was not opened last.
    """
    sub = normalize_archive(
        _zip_bytes(project), archive_kind="zip", limits=_PROJECT_NORMALIZE_LIMITS
    )
    base_snapshot = normalize_archive(_zip_bytes(base), archive_kind="zip")
    screen = mod._screen_snapshot(sub.canonical_zip, sub.manifest, _LANGUAGES)
    extractor = DefaultTextExtractor()
    with zipfile.ZipFile(io.BytesIO(base_snapshot.canonical_zip)) as base_zf:

        def read_text(side: Side, entry: ManifestEntry) -> str | None:
            if side == "sub":
                return screen.texts.get(entry.path)
            return extractor.extract(entry.cls, base_zf.read(entry.path))

        context = build_mentor_context(
            base_manifest=base_snapshot.manifest,
            sub_manifest=sub.manifest,
            delta=compute_delta(base_snapshot.manifest, sub.manifest),
            read_text=read_text,
            base_version=1,
            latest_version=1,
        )
    return context + not_opened_block(screen.not_opened)


class TestTheFixture:
    def test_it_holds_every_class_of_file(self) -> None:
        """What the locks below rest on: each class is there, as named."""
        delta = _delta(PROJECT, BASE)
        assert delta.changed == ("app.py", "logo.png")
        assert delta.new == ("assets/icon.png", "lib/extra.py", "notes.txt")
        assert delta.deleted == ("old.py",)
        sub = normalize_archive(
            _zip_bytes(PROJECT), archive_kind="zip", limits=_PROJECT_NORMALIZE_LIMITS
        )
        classes = {entry.path: entry.cls for entry in sub.manifest.included}
        assert classes["logo.png"] is EntryClass.BINARY
        assert classes["assets/icon.png"] is EntryClass.BINARY
        assert [(e.path, e.reason) for e in sub.manifest.excluded] == [
            (".env", ExcludedReason.MAY_CONTAIN_SECRETS)
        ]


class TestTheStudentsFiles:
    async def test_a_only_the_changed_and_the_new_files_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock a: not the unchanged, deleted, secret, binary or unread ones."""
        reading, _ = await _door(monkeypatch, PROJECT, BASE)

        assert [(f.name, f.text, f.has_lines) for f in reading.files] == [
            ("app.py", PROJECT["app.py"].decode(), True),
            ("lib/extra.py", PROJECT["lib/extra.py"].decode(), True),
        ]
        # The file the screen did not read is named, as before -- and only there.
        assert [(n.arcname, n.reason) for n in reading.not_opened] == [
            (".env", ErrorCategory.MAY_CONTAIN_SECRETS),
            ("notes.txt", ErrorCategory.CHARSET_VIOLATION),
        ]

    async def test_b_a_changed_file_carries_its_base_a_new_one_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock b: the base's text word for word, from the one base snapshot."""
        reading, s3 = await _door(monkeypatch, PROJECT, BASE)

        assert dict(reading.base_files) == {"app.py": BASE["app.py"].decode()}
        assert "lib/extra.py" not in reading.base_files
        assert s3.get_calls == ["base/k"]

    async def test_c_a_document_has_no_lines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock c: a docx or pdf is placed by the file alone, as on the text path."""
        old_report = _docx("Звіт: перша версія")
        reading, _ = await _door(
            monkeypatch,
            {
                "app.py": b"x = 2\n",
                "report.docx": _docx("Звіт: друга версія"),
                "slides.pdf": _pdf("Slides of the project"),
            },
            {"app.py": b"x = 1\n", "report.docx": old_report},
        )

        files = {f.name: f for f in reading.files}
        assert sorted(files) == ["app.py", "report.docx", "slides.pdf"]
        assert files["app.py"].has_lines is True
        assert files["report.docx"].has_lines is False
        assert files["slides.pdf"].has_lines is False
        assert "друга версія" in files["report.docx"].text
        assert "Slides of the project" in files["slides.pdf"].text
        assert reading.base_files["report.docx"] == DefaultTextExtractor().extract(
            EntryClass.DOCUMENT, old_report
        )

    async def test_d_without_a_base_every_read_file_is_the_students(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock d: "all new" -- every file the screen read, and no base."""
        reading, s3 = await _door(monkeypatch, PROJECT)

        assert [f.name for f in reading.files] == ["app.py", "lib/extra.py", "util.py"]
        assert not reading.base_files
        assert s3.get_calls == []

    async def test_a_base_document_that_cannot_be_read_has_no_base_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The submission is not failed for the author's broken document."""
        reading, _ = await _door(
            monkeypatch,
            {"app.py": b"x = 2\n", "report.docx": _docx("It opens now")},
            {"app.py": b"x = 1\n", "report.docx": _zip_bytes({"a.txt": b"no"})},
        )

        assert [f.name for f in reading.files] == ["app.py", "report.docx"]
        assert dict(reading.base_files) == {"app.py": "x = 1\n"}

    async def test_the_same_project_as_its_base_has_no_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(monkeypatch, BASE, BASE)

        assert reading.files == ()
        assert not reading.base_files


class TestTheTextIsUntouched:
    async def test_e_text_and_safety_text_are_the_context_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock e: what the models and the safety check read, byte for byte."""
        reading, _ = await _door(monkeypatch, PROJECT, BASE)
        before = _context_as_before(PROJECT, BASE)

        assert reading.text.encode() == before.encode()
        assert (
            reading.safety_text.encode()
            == before.removesuffix(not_opened_block(reading.not_opened)).encode()
        )
        # The neighbour the context shows is the author's file: not the work.
        assert "path=util.py" in reading.text
        assert "util.py" not in {f.name for f in reading.files}
