"""The new path's door gives the work file by file (mentor-rebuild task 09b, K3).

``PRE-FLIGHT.md`` 9.3 (b): the boundaries of the files are remembered by the
function that assembles the text, and the new path receives them beside it.
The locks:

* the text and what was not opened are exactly what today's Mentor reads —
  its own function is unchanged, and the new one returns the same two;
* each file is its own text, without the frame, in the order of the text;
* a document (docx, pdf) has no lines a student would recognise, so it is
  marked as having none — a quote in it is placed by the file alone.
"""

from __future__ import annotations

import pytest

from course_supporter.homework.criteria_verdicts import WorkFile
from course_supporter.homework.doors import (
    assemble_submission_text,
    assemble_submission_work,
)
from course_supporter.security.archive import ExtractedFile
from course_supporter.security.stage1 import Stage1Result


def _single(filename: str, extension: str, text: str) -> Stage1Result:
    return Stage1Result(
        filename=filename,
        extension=extension,
        detected_mime="text/plain",
        detected_charset="utf-8",
        nfc_text=text,
        archive_entries=None,
        context="homework",
    )


def _archive(*members: tuple[str, str]) -> Stage1Result:
    return Stage1Result(
        filename="work.zip",
        extension="zip",
        detected_mime="application/zip",
        detected_charset=None,
        nfc_text=None,
        archive_entries=tuple(
            ExtractedFile(arcname=name, content=body.encode(), depth=0)
            for name, body in members
        ),
        context="homework",
    )


def _both(result: Stage1Result, filename: str) -> tuple[object, ...]:
    today = assemble_submission_text(result, file_bytes=b"", filename=filename)
    work = assemble_submission_work(result, file_bytes=b"", filename=filename)
    return today, work


class TestASingleFile:
    def test_one_file_with_lines(self) -> None:
        text = "def double(n):\n    return n * 2\n"
        today, (body, not_opened, files) = _both(
            _single("main.py", "py", text), "main.py"
        )

        assert today == (body, not_opened)
        assert files == (WorkFile("main.py", text, True),)

    @pytest.mark.parametrize("extension", ["docx", "pdf"])
    def test_a_document_has_no_lines(self, extension: str) -> None:
        name = f"report.{extension}"
        _, (_, _, files) = _both(_single(name, extension, "Звіт.\nДругий абзац."), name)

        assert files == (WorkFile(name, "Звіт.\nДругий абзац.", False),)


class TestAnArchive:
    def test_each_member_is_its_own_file_in_the_order_of_the_text(self) -> None:
        result = _archive(
            ("src/main.py", "print('ok')\n"),
            ("README.md", "--- src/main.py ---\nnot a frame\n"),
            ("docs/spec.pdf", "Specification"),
        )

        today, (body, not_opened, files) = _both(result, "work.zip")

        assert today == (body, not_opened)
        assert files == (
            WorkFile("src/main.py", "print('ok')\n", True),
            WorkFile("README.md", "--- src/main.py ---\nnot a frame\n", True),
            WorkFile("docs/spec.pdf", "Specification", False),
        )

    def test_a_member_that_did_not_fit_is_not_a_file_of_the_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "course_supporter.homework.text_budget.submission_text_budget_chars",
            lambda: 200,
        )
        result = _archive(("a.py", "a = 1\n"), ("big.py", "x" * 500))

        _, (body, not_opened, files) = _both(result, "work.zip")

        assert [f.name for f in files] == ["a.py"]
        assert [entry.arcname for entry in not_opened] == ["big.py"]
        assert "=== NOT OPENED (1) ===" in body
