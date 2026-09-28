"""The documentation site names every code of a test written in the system (task 07c).

A reader acts on a refusal by its code: the errors page lists it, and its link
leads to the code's own section on the authors' page. Until commit B6 of task
07c nothing read these pages — a code dropped from the errors page left every
test green. The codes are the set the README lock reads
(:func:`tests._helpers.written_test_codes.written_test_codes`), so a new one
reaches both locks. Only the Markdown is read — no build, no database — so this
lock runs wherever the tests run, CI included.

The two refusals of a student's comment (hotfix 6) are held the same way: the
errors page lists them, and the API page — where a school's platform reads
about the comment — has a section for each.

That each link finds its anchor is the site build's to prove: ``mkdocs build
--strict`` fails on a link to an anchor a page does not have
(``validation.links.anchors`` in ``mkdocs.yml``).
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import yaml

# ``homework.submission_core`` imports ``api.upload_validation``, and
# ``api/__init__`` eagerly imports the FastAPI app -- so importing the door
# module first hits a pre-existing circular import (``test_policies.py``).
import course_supporter.api  # noqa: F401
from course_supporter.homework.submission_core import (
    STUDENT_NOTE_REJECTED,
    STUDENT_NOTE_TOO_LONG,
)
from tests._helpers.written_test_codes import written_test_codes

_PAGES = pathlib.Path(__file__).parents[2] / "docs" / "uk"
_ERRORS = _PAGES / "errors" / "index.md"
_AUTHORS = _PAGES / "authors" / "index.md"
_API = _PAGES / "api" / "index.md"

# The first cell of a table row: the code, linked or as it is.
_ROW_CODE = re.compile(r"^\| \[?`([A-Z][A-Z0-9_]+)`", re.MULTILINE)
# An anchor the page declares (attr_list): at the end of a heading, or of a
# table cell. Anywhere else the braces are text, not an anchor.
_ANCHOR = re.compile(r"\{#([A-Z][A-Z0-9_]+)\}(?=[ \t]*(?:\||$))", re.MULTILINE)


def _front_matter(text: str) -> dict[str, Any]:
    """The YAML between a page's two opening ``---`` lines."""
    _, block, _ = text.split("---\n", 2)
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict), "the page opens with its front matter"
    return loaded


class TestTheSiteNamesEveryCodeOfAWrittenTest:
    def test_the_errors_page_lists_every_code(self) -> None:
        listed = _front_matter(_ERRORS.read_text(encoding="utf-8"))["error_codes"]

        assert listed, "the page lists some codes"
        missing = sorted(written_test_codes() - set(listed))
        assert not missing, f"not in error_codes: {missing}"

    def test_the_errors_page_has_a_row_for_every_code(self) -> None:
        rows = set(_ROW_CODE.findall(_ERRORS.read_text(encoding="utf-8")))

        assert rows, "the page has rows of codes"
        missing = sorted(written_test_codes() - rows)
        assert not missing, f"no row on the errors page: {missing}"

    def test_the_authors_page_has_an_anchor_for_every_code(self) -> None:
        anchors = set(_ANCHOR.findall(_AUTHORS.read_text(encoding="utf-8")))

        assert anchors, "the page declares some anchors"
        missing = sorted(written_test_codes() - anchors)
        assert not missing, f"no anchor on the authors' page: {missing}"


class TestTheSiteNamesTheCodesOfAStudentsComment:
    _CODES = frozenset({STUDENT_NOTE_TOO_LONG, STUDENT_NOTE_REJECTED})

    def test_the_errors_page_lists_them_with_a_row_each(self) -> None:
        text = _ERRORS.read_text(encoding="utf-8")

        missing = self._CODES - set(_front_matter(text)["error_codes"])
        assert not missing, f"not in error_codes: {sorted(missing)}"
        missing = self._CODES - set(_ROW_CODE.findall(text))
        assert not missing, f"no row on the errors page: {sorted(missing)}"

    def test_the_api_page_has_an_anchor_for_each(self) -> None:
        anchors = set(_ANCHOR.findall(_API.read_text(encoding="utf-8")))

        missing = self._CODES - anchors
        assert not missing, f"no anchor on the API page: {sorted(missing)}"
