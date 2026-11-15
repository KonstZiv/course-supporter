"""The safety input of a project with a large changed file (project chain K6).

On the new path the safety check of a project reads its student's files alone
(``DoorReading.student_files_text``, chain K4). A changed file larger than the
context's own threshold (``mentor_context.H_C_WHOLE_MAX_BYTES``) -- the one the
reviewing models read as a unified diff -- comes to the check under its frame
with the lines the student added or changed alone: the ``+`` lines of that
diff, without its frames and without a line of the author's. Everything else
comes as before K6, byte for byte: a new file, a changed file at or under the
threshold, and a file whose base version has no text.

Through the real project door (``tests/_helpers/recorded_project.py``), so the
threshold is the context's own, read off the same manifest size.
"""

from __future__ import annotations

import hashlib

import pytest
from tests._helpers.recorded_project import read_project
from tests.unit.test_homework.test_path_stages import _what_safety_reads

from course_supporter.homework.criteria_verdicts import WorkFile
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.mentor_context import H_C_WHOLE_MAX_BYTES
from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.homework.text_budget import ENTRY_FRAME
from course_supporter.models.source import AssignmentType

_PROJECT_KEY = PathKey(AssignmentType.PROJECT, SubmissionState.FIRST)


def _line(name: str, number: int) -> str:
    """One line of the author's module: incompressible, so the archive is no bomb."""
    digest = hashlib.sha256(f"{name}{number}".encode()).hexdigest()
    return f'{name}_{number:05d} = "{digest}"\n'


def _module(name: str, *, at_least: int) -> list[str]:
    """The lines of a module of at least ``at_least`` bytes."""
    lines: list[str] = []
    size = 0
    while size < at_least:
        lines.append(_line(name, len(lines)))
        size += len(lines[-1])
    return lines


def _padded(lines: list[str], size: int) -> str:
    """``lines`` with a last comment line making the text exactly ``size`` bytes."""
    text = "".join(lines)
    pad = size - len(text) - len("#\n")
    assert pad >= 0
    return text + "#" + "x" * pad + "\n"


def _frame(name: str, body: str) -> str:
    return ENTRY_FRAME.format(name=name) + body


def _before_k6(reading: DoorReading) -> str:
    """The safety input as chain K4 built it: every file of the student's whole."""
    return "\n".join(_frame(file.name, file.text) for file in reading.files)


# ── The project: one large changed file, one small, one new ────────────────

_AUTHORS_BIG = _module("table", at_least=H_C_WHOLE_MAX_BYTES + 4096)
_CHANGED_LINE = 'table_00010 = "the student\'s own value"\n'
_ADDED = ["def lookup(key):\n", "    return globals()[key]\n"]
_STUDENTS_BIG = [*_AUTHORS_BIG[:10], _CHANGED_LINE, *_AUTHORS_BIG[11:], *_ADDED]

_AUTHORS_APP = "def main():\n    print('hello')\n"
_STUDENTS_APP = "def main():\n    print('hello, world')\n"
_STATS = "def mean(xs):\n    return sum(xs) / len(xs)\n"


@pytest.fixture()
async def reading(monkeypatch: pytest.MonkeyPatch) -> DoorReading:
    return await read_project(
        monkeypatch,
        base={
            "app.py": _AUTHORS_APP.encode(),
            "big.py": "".join(_AUTHORS_BIG).encode(),
        },
        project={
            "app.py": _STUDENTS_APP.encode(),
            "big.py": "".join(_STUDENTS_BIG).encode(),
            "stats.py": _STATS.encode(),
        },
    )


class TestALargeChangedFile:
    async def test_comes_by_the_students_lines_alone(
        self, monkeypatch: pytest.MonkeyPatch, reading: DoorReading
    ) -> None:
        text = await _what_safety_reads(monkeypatch, _PROJECT_KEY, reading)

        students = _CHANGED_LINE + "".join(_ADDED)
        assert text == "\n".join(
            [
                _frame("app.py", _STUDENTS_APP),
                _frame("big.py", students),
                _frame("stats.py", _STATS),
            ]
        )
        assert reading.student_files_text == text

    async def test_carries_no_line_of_the_authors_and_no_frame_of_the_diff(
        self, reading: DoorReading
    ) -> None:
        text = reading.student_files_text

        assert not any(line in text for line in _AUTHORS_BIG)
        assert "@@" not in text
        assert "--- base/" not in text
        assert "+++ sub/" not in text
        assert "\n+" not in text
        # The reviewing models still read the diff -- with the author's lines
        # around the change, as its context.
        assert "type=CHANGED-DIFF path=big.py" in reading.text
        assert "\n " + _AUTHORS_BIG[9].rstrip("\n") + "\n" in reading.text

    async def test_is_the_file_the_context_shows_as_a_diff(
        self, reading: DoorReading
    ) -> None:
        assert reading.shown_as_diff == frozenset({"big.py"})
        assert "type=CHANGED-FULL path=app.py" in reading.text

    async def test_the_rest_comes_byte_for_byte_as_before(
        self, reading: DoorReading
    ) -> None:
        """A new file and a changed file under the threshold: as chain K4 had them."""
        before = _before_k6(reading).split("\n--- big.py ---\n")
        now = reading.student_files_text.split("\n--- big.py ---\n")

        assert now[0].encode() == before[0].encode()
        assert now[1].split("\n--- stats.py ---\n")[1].encode() == (
            before[1].split("\n--- stats.py ---\n")[1].encode()
        )


class TestTheThreshold:
    """The context's own: at it, whole; one byte over, by the student's lines."""

    @pytest.mark.parametrize(
        ("size", "by_lines"),
        [(H_C_WHOLE_MAX_BYTES, False), (H_C_WHOLE_MAX_BYTES + 1, True)],
        ids=["at", "over"],
    )
    async def test_the_same_size_the_context_shows_a_diff_by(
        self, monkeypatch: pytest.MonkeyPatch, size: int, by_lines: bool
    ) -> None:
        authors = _module("edge", at_least=H_C_WHOLE_MAX_BYTES - 200)
        students = _padded([*authors, "ANSWER = 42\n"], size)
        reading = await read_project(
            monkeypatch,
            base={"edge.py": "".join(authors).encode()},
            project={"edge.py": students.encode()},
        )

        added = students.removeprefix("".join(authors))
        assert reading.student_files_text == _frame(
            "edge.py", added if by_lines else students
        )
        assert ("type=CHANGED-DIFF path=edge.py" in reading.text) is by_lines
        assert reading.shown_as_diff == (
            frozenset({"edge.py"}) if by_lines else frozenset()
        )


class TestWithoutTheBasesText:
    def test_the_file_comes_whole(self) -> None:
        """No text of the base's version (a binary there, an unreadable one):
        nothing to take the student's lines against, so the file comes whole."""
        body = "".join(_STUDENTS_BIG)
        reading = DoorReading(
            text="context",
            files=(WorkFile("big.py", body, True),),
            shown_as_diff=frozenset({"big.py"}),
        )

        assert reading.student_files_text == _frame("big.py", body)
