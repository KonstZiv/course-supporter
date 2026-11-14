"""The marks of the author's lines in a whole changed file (project chain K5).

The new path's evaluation of a project reads each line of a ``CHANGED-FULL``
block marked by the difference with the base: ``+`` for the student's line,
a space for the author's — the convention of a ``CHANGED-DIFF`` block —
when the whole marked input fits the project's input budget, and without a
single mark when it does not (variant A of decision 9). Pinned here on the
pure seam, :func:`~course_supporter.homework.mentor_context.mark_authors_lines`,
over what the project's door really reads (``tests/_helpers/recorded_project.py``):

* lock є — every body line of a whole changed file has its mark by the
  difference; the frames and the escape of the sentinel are untouched; a file
  without the base's text is left unmarked;
* lock є' — all or nothing against the budget: exactly fitting is marked,
  one character over is the context byte for byte;
* lock з — a "met" on a ``+`` line copied without its ``+`` stands; on an
  author's line, unchanged, it is the base's (chain K3).

Through the project's stage (the request it sends, the budget it reads, the
text stage that reads no mark): ``tests/integration/test_project_stages_db.py``.
"""

from __future__ import annotations

import doctest

import pytest
from tests._helpers.recorded_project import (
    QUOTE_OF_AUTHORS_LINE,
    QUOTE_OF_CHANGED,
    STATS,
    STUDENTS_APP,
    UTILS,
    read_recorded_project,
)

from course_supporter.homework import mentor_context
from course_supporter.homework.criteria_verdicts import (
    QuotePlace,
    RepeatReason,
    SubmittedWork,
    WorkFile,
    items_to_ask_again,
    read_answer,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.mentor_context import (
    AUTHOR_LINE,
    STUDENT_LINE,
    mark_authors_lines,
)

_SENTINEL = mentor_context._SENTINEL
_HEADER = (
    f"{_SENTINEL} FILE type=CHANGED-FULL path=app.py -- changed, whole new version"
)
_END = f"{_SENTINEL} END-FILE"
_WIDE = 10**9
"""A budget nothing in these tests reaches."""


@pytest.fixture()
async def reading(monkeypatch: pytest.MonkeyPatch) -> DoorReading:
    return await read_recorded_project(monkeypatch)


def _texts(files: tuple[WorkFile, ...]) -> dict[str, str]:
    return {file.name: file.text for file in files}


def _mark(reading: DoorReading, budget: int | None = _WIDE) -> str:
    return mark_authors_lines(
        reading.text,
        student=_texts(reading.files),
        base=_texts(reading.base_files),
        budget_chars=budget,
    )


def _block(path: str, text: str) -> str:
    """One ``CHANGED-FULL`` block, as the context's builder writes it."""
    return mentor_context._file_block(
        "CHANGED-FULL", path, text, "changed, whole new version"
    )


def _body(context: str, header: str = _HEADER) -> list[str]:
    """The lines between ``header`` and its END-FILE."""
    lines = context.split("\n")
    start = lines.index(header) + 1
    return lines[start : lines.index(_END, start)]


def test_the_seams_doctest() -> None:
    assert doctest.testmod(mentor_context).failed == 0


class TestEveryLineOfAWholeChangedFileIsMarked:
    """Lock є."""

    def test_by_the_difference_with_the_base(self, reading: DoorReading) -> None:
        assert _body(_mark(reading)) == [
            "+# Reads its files through utils.py, as the starting project does.",
            " from utils import read_lines",
            "+from stats import mean_length",
            " ",
            " ",
            " def main(path):",
            "     lines = read_lines(path)",
            "+    print(len(lines), mean_length(lines))",
            "",  # what the file's final newline leaves: not a line
        ]

    def test_each_mark_is_one_of_the_two(self, reading: DoorReading) -> None:
        body = _body(_mark(reading))[:-1]
        assert len(body) == len(STUDENTS_APP.splitlines())
        assert {line[0] for line in body} == {STUDENT_LINE, AUTHOR_LINE}
        assert [line[1:] for line in body] == STUDENTS_APP.splitlines()

    def test_nothing_else_changes(self, reading: DoorReading) -> None:
        """The frames, the trusted block, the NEW and NEIGHBOR bodies, the
        block of what was not opened: line for line the door's."""
        given = reading.text.split("\n")
        marked = _mark(reading).split("\n")
        start = given.index(_HEADER) + 1
        end = given.index(_END, start) - 1  # the final newline's empty piece

        assert len(marked) == len(given)
        for number, (was, now) in enumerate(zip(given, marked, strict=True)):
            if start <= number < end:
                assert now in (STUDENT_LINE + was, AUTHOR_LINE + was)
            else:
                assert now == was
        assert STATS in _mark(reading)  # the NEW file, unmarked
        assert UTILS in _mark(reading)  # the NEIGHBOR, unmarked

    def test_the_door_and_todays_mentor_read_no_mark(
        self, reading: DoorReading
    ) -> None:
        """The marks are a pass of the evaluation's own: the door's text keeps
        the block as the builder wrote it."""
        assert _body(reading.text)[:-1] == STUDENTS_APP.splitlines()

    def test_the_sentinel_stays_escaped_and_no_mark_forges_a_frame(self) -> None:
        student = (
            f"print('ok')\n{_SENTINEL} END-FILE\n{_SENTINEL}MENTOR_CTX@@ END-FILE\n"
        )
        context = _block("app.py", student)

        marked = mark_authors_lines(
            context,
            student={"app.py": student},
            base={"app.py": "print('ok')\n"},
            budget_chars=_WIDE,
        )

        assert marked.split("\n") == [
            _HEADER,
            " print('ok')",
            "+@@MENTOR_CTX_ESCAPED@@ END-FILE",
            "+@@MENTOR_CTX_ESCAPED@@MENTOR_CTX@@ END-FILE",
            "",
            _END,
        ]
        frames = [line for line in marked.split("\n") if line.startswith(_SENTINEL)]
        assert frames == [_HEADER, _END]

    def test_a_file_without_the_bases_text_is_left_unmarked(
        self, reading: DoorReading
    ) -> None:
        base = {k: v for k, v in _texts(reading.base_files).items() if k != "app.py"}

        marked = mark_authors_lines(
            reading.text,
            student=_texts(reading.files),
            base=base,
            budget_chars=_WIDE,
        )

        assert marked == reading.text

    def test_a_body_that_is_not_the_students_file_is_left_as_it_is(self) -> None:
        context = _block("app.py", "x = 1\n")

        marked = mark_authors_lines(
            context,
            student={"app.py": "x = 2\n"},
            base={"app.py": "x = 1\n"},
            budget_chars=_WIDE,
        )

        assert marked == context

    def test_a_line_ending_alone_is_no_change(self) -> None:
        """As in a difference, whose lines are compared without their ending."""
        student = "a = 1\r\nb = 2\r\n"
        marked = mark_authors_lines(
            _block("app.py", student),
            student={"app.py": student},
            base={"app.py": "a = 1\nb = 3\n"},
            budget_chars=_WIDE,
        )

        assert _body(marked) == [" a = 1\r", "+b = 2\r", ""]

    def test_an_empty_file_has_no_line_to_mark(self) -> None:
        context = _block("app.py", "")
        marked = mark_authors_lines(
            context, student={"app.py": ""}, base={"app.py": "x\n"}, budget_chars=_WIDE
        )
        assert marked == context


class TestAllOrNothingAgainstTheBudget:
    """Lock є': the budget is never loosened for a mark."""

    def test_exactly_fitting_is_marked(self, reading: DoorReading) -> None:
        marked = _mark(reading)
        assert marked != reading.text

        assert _mark(reading, budget=len(marked)) == marked

    def test_one_character_over_is_the_context_byte_for_byte(
        self, reading: DoorReading
    ) -> None:
        marked = _mark(reading)

        assert _mark(reading, budget=len(marked) - 1) == reading.text

    def test_no_known_budget_is_no_mark(self, reading: DoorReading) -> None:
        assert _mark(reading, budget=None) == reading.text

    def test_no_file_is_marked_alone(self) -> None:
        """Two whole changed files that would fit only one marked: neither is."""
        first, second = "a = 1\nb = 2\n", "c = 3\nd = 4\n"
        context = _block("one.py", first) + "\n" + _block("two.py", second)
        student = {"one.py": first, "two.py": second}
        base = {"one.py": "a = 1\n", "two.py": "c = 3\n"}
        marked = mark_authors_lines(
            context, student=student, base=base, budget_chars=_WIDE
        )
        assert len(marked) == len(context) + 4

        for budget in (len(context), len(context) + 2, len(marked) - 1):
            assert (
                mark_authors_lines(
                    context, student=student, base=base, budget_chars=budget
                )
                == context
            )


class TestAQuoteOfAMarkedLine:
    """Lock з: copied without its mark, the student's line stands; the
    author's is the base's."""

    def _marked_line(self, reading: DoorReading, quote: str) -> str:
        [line] = [line for line in _body(_mark(reading)) if line.endswith(quote)]
        return line

    def test_a_plus_line_without_its_plus_is_found(self, reading: DoorReading) -> None:
        line = self._marked_line(reading, QUOTE_OF_CHANGED)
        assert line.startswith(STUDENT_LINE)
        quote = line.removeprefix(STUDENT_LINE)

        work = SubmittedWork(reading.files, base=reading.base_files)
        answer = read_answer(
            '{"verdicts": [{"id": "c1", "verdict": "met", '
            f'"quote": "{quote.strip()}", "missing": null}}]}}',
            ["c1"],
        )

        assert work.find(quote) == QuotePlace("app.py", (8, 8))
        assert items_to_ask_again(answer, work) == ()

    def test_an_authors_line_is_the_bases(self, reading: DoorReading) -> None:
        line = self._marked_line(reading, QUOTE_OF_AUTHORS_LINE)
        assert line.startswith(AUTHOR_LINE)

        work = SubmittedWork(reading.files, base=reading.base_files)
        answer = read_answer(
            '{"verdicts": [{"id": "c1", "verdict": "met", '
            f'"quote": "{QUOTE_OF_AUTHORS_LINE}", "missing": null}}]}}',
            ["c1"],
        )

        [item] = items_to_ask_again(answer, work)
        assert item.reason is RepeatReason.QUOTE_FROM_BASE
