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
about the comment — has a section for each. So are the refusals of the
criteria routes (task 08): the errors page lists each with a row that leads to
its anchor on the authors' page.

Task 09b adds two. The authors' page names every state of a reading without a
list and every reason code, and quotes every sentence the reading carries word
for word: the texts were approved as they stand in the code, and a page that
retold them would drift unseen. The errors page lists the reason a text task's
review fails with, ``review_parts_missing``, with a row.

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
from course_supporter.homework.criteria_edit_service import (
    AWAITING_FIRST_SUBMISSION_MESSAGE,
    COMPOSING_MESSAGE,
    NOT_COMPOSED_MESSAGES,
    CriteriaReasonCode,
    CriteriaRefusalCode,
    CriteriaStatus,
)
from course_supporter.homework.submission_core import (
    STUDENT_NOTE_REJECTED,
    STUDENT_NOTE_TOO_LONG,
)
from course_supporter.homework.text_result import REVIEW_PARTS_MISSING
from tests._helpers.written_test_codes import written_test_codes

_PAGES = pathlib.Path(__file__).parents[2] / "docs" / "uk"
_ERRORS = _PAGES / "errors" / "index.md"
_AUTHORS = _PAGES / "authors" / "index.md"
_API = _PAGES / "api" / "index.md"

# The first cell of a table row: the code, linked or as it is.
_ROW_CODE = re.compile(r"^\| \[?`([A-Z][A-Z0-9_]+)`", re.MULTILINE)
# The first cell of a row whose code is a lower-case reason of the webhook's
# ``failed`` event, linked or as it is.
_ROW_REASON = re.compile(r"^\| \[?`([a-z][a-z0-9_]+)`", re.MULTILINE)
# The first cell of a row whose code links to its own anchor on the authors'
# page. ``TASK_NOT_READY`` also has an unlinked row among the answer-key codes,
# which :data:`_ROW_CODE` would count for the criteria routes too.
_ROW_LINKED_TO_AUTHORS = re.compile(
    r"^\| \[`([A-Z][A-Z0-9_]+)`\]\(\.\./authors/index\.md#\1\)", re.MULTILINE
)
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


class TestTheSiteNamesEveryCodeOfTheCriteriaRoutes:
    _CODES = frozenset(code.value for code in CriteriaRefusalCode)

    def test_the_errors_page_lists_them_with_a_linked_row_each(self) -> None:
        text = _ERRORS.read_text(encoding="utf-8")

        assert self._CODES, "the vocabulary under test is not empty"
        missing = self._CODES - set(_front_matter(text)["error_codes"])
        assert not missing, f"not in error_codes: {sorted(missing)}"
        missing = self._CODES - set(_ROW_LINKED_TO_AUTHORS.findall(text))
        assert not missing, f"no linked row on the errors page: {sorted(missing)}"

    def test_the_authors_page_has_an_anchor_for_each(self) -> None:
        anchors = set(_ANCHOR.findall(_AUTHORS.read_text(encoding="utf-8")))

        missing = self._CODES - anchors
        assert not missing, f"no anchor on the authors' page: {sorted(missing)}"


class TestTheSiteSaysWhyATaskHasNoList:
    def test_the_authors_page_names_every_state_and_reason(self) -> None:
        text = _AUTHORS.read_text(encoding="utf-8")
        codes = {status.value for status in CriteriaStatus} | {
            reason.value for reason in CriteriaReasonCode
        }

        assert codes, "the vocabulary under test is not empty"
        missing = sorted(code for code in codes if f"`{code}`" not in text)
        assert not missing, f"not on the authors' page: {missing}"

    def test_the_authors_page_gives_each_sentence_word_for_word(self) -> None:
        text = _AUTHORS.read_text(encoding="utf-8")
        # Each advice in the row of its own reason: a page that kept every
        # sentence but under the wrong code would mislead as surely as one
        # that retold them.
        rows = [
            f"| `{reason.value}` | {NOT_COMPOSED_MESSAGES[reason]} |"
            for reason in CriteriaReasonCode
        ]
        quotes = [f"> {AWAITING_FIRST_SUBMISSION_MESSAGE}", f"> {COMPOSING_MESSAGE}"]

        assert rows, "there are reasons to advise on"
        missing = [line[:60] for line in (*rows, *quotes) if line not in text]
        assert not missing, f"not word for word on the authors' page: {missing}"


class TestTheSiteNamesTheFailureOfATextTasksReview:
    def test_the_errors_page_lists_it_with_a_row(self) -> None:
        text = _ERRORS.read_text(encoding="utf-8")
        rows = set(_ROW_REASON.findall(text))

        assert REVIEW_PARTS_MISSING in _front_matter(text)["error_codes"]
        assert rows, "the page has rows of lower-case reasons"
        assert REVIEW_PARTS_MISSING in rows, f"no row on the errors page: {rows}"
