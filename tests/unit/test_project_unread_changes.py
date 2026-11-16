"""A project changed without a file the doors could read (chain K6).

``DoorReading.files`` empty used to mean one thing to the new path: nothing of
the student's to review, refused as ``no_changes``. It also happened when the
student did add or change files and the doors could read none of them -- an
encoding not established, a document that would not open, binary files alone,
or a project without a base holding no text file. The project door now names
those files (``DoorReading.unread_changes``), the new path refuses such a
project as ``no_readable_changes`` with the list in ``safety_result``, and the
portal reads back the code and the list. The whole run through the worker is
in ``tests/integration/test_project_no_changes_db.py``.

* lock u1 -- the list: each new or changed file without text, with its
  reason, by path; a binary one with ``forbidden_type`` and its size;
* lock u2 -- not the author's: an unreadable file the student left as it was,
  and a secret excluded from the snapshot, stay out of it;
* lock u3 -- the record: the category, and the list in the shape Stage 1
  stores what it did not open;
* lock u4 -- the portal: the code without details, the list as any other.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from course_supporter.api.routes._portal_shared import (
    curated_not_opened,
    curated_rejection,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.project_submission import (
    NO_READABLE_CHANGES,
    persist_no_readable_changes,
)
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.security.schemas import NotOpenedEntry
from tests.unit.test_project_door_files import BASE, PROJECT, _door
from tests.unit.test_project_submission import _FakeHwRepo, _FakeSession, _zip_bytes

_PNG = BASE["logo.png"]
_CP1251 = "Розв'язок\n".encode("cp1251")
# A zip by its magic, so the snapshot keeps it, but not a document inside.
_BROKEN_DOCX = _zip_bytes({"a.txt": b"no"})


def _unread(reading: DoorReading) -> list[tuple[str, ErrorCategory, int]]:
    return [(e.arcname, e.reason, e.size) for e in reading.unread_changes]


class TestTheList:
    """Lock u1."""

    async def test_every_new_or_changed_file_without_text_by_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(monkeypatch, PROJECT, BASE)

        assert _unread(reading) == [
            (
                "assets/icon.png",
                ErrorCategory.FORBIDDEN_TYPE,
                len(PROJECT["assets/icon.png"]),
            ),
            ("logo.png", ErrorCategory.FORBIDDEN_TYPE, len(PROJECT["logo.png"])),
            ("notes.txt", ErrorCategory.CHARSET_VIOLATION, len(PROJECT["notes.txt"])),
        ]
        # The files that were read are where they were.
        assert [f.name for f in reading.files] == ["app.py", "lib/extra.py"]

    async def test_a_document_that_would_not_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(
            monkeypatch, {**BASE, "report.docx": _BROKEN_DOCX}, BASE
        )

        assert reading.files == ()
        assert _unread(reading) == [
            ("report.docx", ErrorCategory.MAGIC_MISMATCH, len(_BROKEN_DOCX))
        ]

    async def test_without_a_base_every_binary_file_is_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(monkeypatch, {"b.png": _PNG, "a.png": _PNG})

        assert reading.files == ()
        assert _unread(reading) == [
            ("a.png", ErrorCategory.FORBIDDEN_TYPE, len(_PNG)),
            ("b.png", ErrorCategory.FORBIDDEN_TYPE, len(_PNG)),
        ]

    async def test_nothing_unread_when_nothing_changed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(monkeypatch, dict(BASE), BASE)

        assert (reading.files, reading.unread_changes) == ((), ())


class TestNotTheAuthors:
    """Lock u2."""

    async def test_an_unreadable_file_left_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = {**BASE, "notes.txt": _CP1251}
        reading, _ = await _door(monkeypatch, dict(base), base)

        # Named as not opened, as before -- but it is not the student's change.
        assert [e.arcname for e in reading.not_opened] == ["notes.txt"]
        assert reading.unread_changes == ()

    async def test_a_secret_excluded_from_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(
            monkeypatch, {**BASE, ".env": b"API_KEY=sk-live-secret\n"}, BASE
        )

        assert [e.arcname for e in reading.not_opened] == [".env"]
        assert reading.unread_changes == ()


_ENTRIES = (
    NotOpenedEntry(arcname="app.py", reason=ErrorCategory.CHARSET_VIOLATION, size=40),
    NotOpenedEntry(arcname="logo.png", reason=ErrorCategory.FORBIDDEN_TYPE, size=264),
)


async def _persisted() -> _FakeHwRepo:
    repo = _FakeHwRepo()
    await persist_no_readable_changes(
        _FakeSession(),  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        uuid.uuid4(),
        _ENTRIES,
    )
    return repo


class TestTheRecord:
    """Lock u3."""

    async def test_rejected_with_the_category_and_the_list(self) -> None:
        repo = await _persisted()

        assert repo.status == "rejected"
        assert repo.status_error is not None
        assert repo.status_error.startswith(f"{NO_READABLE_CHANGES}: ")
        assert repo.safety == {
            "source": "normalizer",
            "reason": repo.status_error,
            "category": "no_readable_changes",
            "not_opened": [
                {"arcname": "app.py", "reason": "charset_violation", "size": 40},
                {"arcname": "logo.png", "reason": "forbidden_type", "size": 264},
            ],
        }


class TestThePortal:
    """Lock u4."""

    async def test_the_code_without_details_and_the_list(self) -> None:
        repo = await _persisted()
        submission = MagicMock()
        submission.status = "rejected"
        submission.safety_result = repo.safety
        submission.original_filename = "proj.zip"

        rejection = curated_rejection(submission)
        assert rejection is not None
        assert (rejection.code, rejection.details) == ("no_readable_changes", None)
        assert [(e.path, e.reason, e.size) for e in curated_not_opened(submission)] == [
            ("app.py", "charset_violation", 40),
            ("logo.png", "forbidden_type", 264),
        ]
