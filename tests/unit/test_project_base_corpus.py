"""The project's door reads the author's whole base for the quotes (chain K3).

The evaluation tells a quote of the author's code from the student's by the
text of every file of the base (``DoorReading.base_files``). The door reads it
once per submission, from the base snapshot the delta context was built from,
only after the size check, as the student's files are read (the encoding
recovered, NFC) -- and only for the new path: today's Mentor does not ask.

Locks of the K3 brief held here: c (a renamed file, through the door), g (a
base file in cp1251), the door's part of the size lock (no read over the
limit, exactly one otherwise) and of e (today's Mentor reads no base).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from course_supporter.homework import project_submission as mod
from course_supporter.homework.criteria_verdicts import (
    RepeatItem,
    RepeatReason,
    SubmittedWork,
    items_to_ask_again,
    read_answer,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.project_submission import process_project_submission
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from course_supporter.security.policies import HOMEWORK_POLICY
from course_supporter.security.text_screen import read_as_screened, screen_text
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

# A file of the author's in cp1251, long enough for its encoding to be
# established (``charset_recovery.MIN_RECOVERABLE_BYTES``).
_CP1251_TEXT = (
    "# Модуль рахує середнє значення списку чисел, які ввів користувач.\n"
    "# Порожній список означає, що рахувати нічого: функція повертає нуль.\n"
    "# Автор курсу залишив цей файл як основу для домашнього завдання.\n"
    "def average(values):\n"
    "    return sum(values) / len(values) if values else 0\n"
)
_AUTHORS_CYRILLIC = (
    "# Порожній список означає, що рахувати нічого: функція повертає нуль."
)

_SOLVE = b"def solve(numbers):\n    return sorted(set(numbers))\n"
BASE: dict[str, bytes] = {
    "app.py": b"def main():\n    print('hello')\n",
    "stats.py": _CP1251_TEXT.encode("cp1251"),
    "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
}
# The student re-saved stats.py in UTF-8 and added a function; app.py is new.
PROJECT: dict[str, bytes] = {
    "app.py": b"def main():\n    print('hello, student')\n",
    "stats.py": (
        _CP1251_TEXT + "\ndef median(values):\n    return values[0]\n"
    ).encode(),
    "logo.png": BASE["logo.png"],
}


class _Reads:
    """Counts the reads of the base corpus, then reads it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = mod._read_base_corpus

        def _counted(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(mod, "_read_base_corpus", _counted)


async def _door(
    monkeypatch: pytest.MonkeyPatch,
    project: dict[str, bytes],
    base: dict[str, bytes],
    *,
    todays_mentor: bool = False,
    budget: int = 1_000_000,
) -> tuple[DoorReading | None, _FakeS3]:
    """The project's door with ``base`` attached as its stored snapshot.

    The new path asks for the base; today's Mentor calls the door as it
    always has, without the argument.
    """
    monkeypatch.setattr(mod, "project_context_budget_chars", lambda: budget)
    snapshot = normalize_archive(_zip_bytes(base), archive_kind="zip")
    stored = _FakeBase(
        version=1, snapshot_key="base/k", manifest=manifest_to_jsonb(snapshot.manifest)
    )
    monkeypatch.setattr(
        mod, "ProjectBaseRepository", lambda session: _FakeRepo(stored, stored)
    )
    s3 = _FakeS3(base_snapshot_zip=snapshot.canonical_zip)
    asks = {} if todays_mentor else {"base_corpus": True}
    reading = await process_project_submission(
        session=_FakeSession(),  # type: ignore[arg-type]
        s3=s3,  # type: ignore[arg-type]
        hw_repo=_FakeHwRepo(),  # type: ignore[arg-type]
        submission=_FakeSubmission(
            base_id=uuid.uuid4(), authored_document_id=uuid.uuid4()
        ),  # type: ignore[arg-type]
        sid=uuid.uuid4(),
        jid=uuid.uuid4(),
        file_bytes=_zip_bytes(project),
        raw_key="homework/t/s/proj.zip",
        languages=_LANGUAGES,
        **asks,
    )
    return reading, s3


def _asked_again(reading: DoorReading, quote: str) -> tuple[RepeatItem, ...]:
    """What the evaluation would ask again of a "met" on ``quote``."""
    work = SubmittedWork(reading.files, base=reading.base_files.values())
    content = json.dumps(
        {"verdicts": [{"id": "c1", "verdict": "met", "quote": quote, "missing": None}]}
    )
    return items_to_ask_again(read_answer(content, ["c1"]), work)


class TestTheCorpus:
    async def test_every_text_file_of_the_base_and_no_binary_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reading, _ = await _door(monkeypatch, PROJECT, BASE)

        assert reading is not None
        assert sorted(reading.base_files) == ["app.py", "stats.py"]
        assert reading.base_files["app.py"] == BASE["app.py"].decode()

    async def test_c_a_renamed_file_through_the_door(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock c: base a.py, work b.py of the same text, a.py deleted."""
        reading, _ = await _door(monkeypatch, {"b.py": _SOLVE}, {"a.py": _SOLVE})

        assert reading is not None
        assert [f.name for f in reading.files] == ["b.py"]
        assert dict(reading.base_files) == {"a.py": _SOLVE.decode()}
        assert _asked_again(reading, "return sorted(set(numbers))") == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )

    async def test_g_a_cp1251_file_of_the_base_reads_as_the_students(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock g: a Cyrillic quote of the author's cp1251 file is the author's."""
        reading, _ = await _door(monkeypatch, PROJECT, BASE)

        assert reading is not None
        assert reading.base_files["stats.py"] == _CP1251_TEXT
        assert _asked_again(reading, _AUTHORS_CYRILLIC) == (
            RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE),
        )
        # The student's own line of the same file stands.
        assert _asked_again(reading, "def median(values):") == ()

    def test_the_base_is_read_as_the_screen_reads_the_students_files(self) -> None:
        """One reading on both sides: the screen's text, without the screen."""
        for raw in (BASE["stats.py"], PROJECT["stats.py"], "Café\n".encode()):
            screened = screen_text(
                raw,
                name="f.py",
                mode=HOMEWORK_POLICY.text_screen_mode,
                languages=_LANGUAGES,
            )
            assert (
                read_as_screened(raw, name="f.py", languages=_LANGUAGES)
                == screened.text
            )

    async def test_a_broken_base_document_is_logged_and_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with capture_logs() as logs:
            reading, _ = await _door(
                monkeypatch,
                {"app.py": b"x = 2\n", "report.docx": _docx("It opens now")},
                {"app.py": b"x = 1\n", "report.docx": _zip_bytes({"a.txt": b"no"})},
            )

        assert reading is not None
        assert dict(reading.base_files) == {"app.py": "x = 1\n"}
        assert [
            (log["event"], log["log_level"], log["path"])
            for log in logs
            if log["event"] == "project_submission.base_document_unread"
        ] == [("project_submission.base_document_unread", "warning", "report.docx")]

    async def test_a_base_text_of_no_established_encoding_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Too short to tell its encoding: unread, as the student's would be."""
        reading, _ = await _door(
            monkeypatch,
            {"app.py": b"x = 2\n"},
            {"app.py": b"x = 1\n", "notes.txt": "Розв'язок\n".encode("cp1251")},
        )

        assert reading is not None
        assert dict(reading.base_files) == {"app.py": "x = 1\n"}


class TestOnceAndAfterTheSizeCheck:
    """The door's part of the size lock."""

    async def test_a_submission_read_once_from_the_one_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = _Reads(monkeypatch)

        reading, s3 = await _door(monkeypatch, PROJECT, BASE)

        assert reading is not None
        assert reads.calls == 1
        # S3 is asked for the base snapshot once: the context's fetch serves both.
        assert s3.get_calls == ["base/k"]

    async def test_a_submission_over_the_limit_does_not_read_the_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = _Reads(monkeypatch)

        reading, _ = await _door(monkeypatch, PROJECT, BASE, budget=10)

        assert reading is None
        assert reads.calls == 0


class TestTodaysMentor:
    async def test_e_reads_no_base_and_the_same_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock e, the door's part: nothing new is read; the text is the same."""
        reads = _Reads(monkeypatch)
        new_path, _ = await _door(monkeypatch, PROJECT, BASE)
        reads.calls = 0

        today, s3 = await _door(monkeypatch, PROJECT, BASE, todays_mentor=True)

        assert today is not None
        assert new_path is not None
        assert reads.calls == 0
        assert not today.base_files
        assert s3.get_calls == ["base/k"]
        assert today.text.encode() == new_path.text.encode()
        assert today.safety_text.encode() == new_path.safety_text.encode()
        assert (today.not_opened, today.flags, today.files) == (
            new_path.not_opened,
            new_path.flags,
            new_path.files,
        )
