"""The note that a review repeats an earlier attempt's (task 13, K3).

Two locks:

* д — a review stored without ``unchanged_from_attempt`` (every review before
  K3, and every review written since for its own submission) reads, prints and
  passes ``curated_structure`` as before: no field gained in what is stored or
  sent, the same bytes of markdown;
* the note itself — the phrase ``review.unchanged`` opens the review, with the
  number put in, in every language of the phrasebook.

The repeat as a whole — the copy, the receipt, nothing paid — is locked end to
end in ``tests/integration/test_attempt_dedup_db.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from course_supporter.api.routes._portal_shared import curated_structure
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.webhook import build_reviewed_payload
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import ReviewStructureV1, Verdict
from course_supporter.phrasebook import load_phrasebook, phrases_for
from tests.unit.test_review_assembler import SNAPSHOTS, STORED_BEFORE_TASK_10

_PHRASEBOOK = Path(__file__).resolve().parents[2] / "config" / "phrasebook"
_LANGUAGES = sorted(load_phrasebook(_PHRASEBOOK))

# A text task's review as the builder of main stores it: every key its dump
# has, a course link with every field task 10 gave a reference.
_STORED_ON_MAIN: dict[str, Any] = {
    "schema_version": "1",
    "language": "ukr",
    "verdict": {"passed": True, "why": "Функція solve є й подвоює число."},
    "test": None,
    "fixed": [],
    "new_remarks": [
        {
            "what": "Немає докстрінга.",
            "why": "Без нього не видно, що функція робить.",
            "todo": "Додайте докстрінг.",
            "read": [
                {
                    "title": "Документування коду",
                    "url": None,
                    "position": {"kind": "audio", "value": "12:05"},
                    "excerpt": "Докстрінг — перший рядок функції.",
                    "segment_id": "0192f6a0-0000-7000-8000-000000000001",
                    "later_in_course": True,
                }
            ],
            "position": None,
        }
    ],
    "open": [],
    "broken": [],
    "mentor_voice": "Назва функції говорить сама за себе.",
    "replies": [],
    "verification": None,
    "progress": None,
}


def _submission(review_result: dict[str, Any]) -> MagicMock:
    submission = MagicMock()
    submission.id = uuid.uuid4()
    submission.score = 50
    submission.review_result = review_result
    submission.review_markdown = "markdown"
    submission.response_language = "uk"
    return submission


def _passed(language: str = "ukr", **note: Any) -> ReviewStructureV1:
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language=language,
        verdict=Verdict(passed=True, why="Everything asked for is there."),
        **note,
    )


@pytest.mark.parametrize(
    "stored",
    [STORED_BEFORE_TASK_10, _STORED_ON_MAIN],
    ids=["before_task_10", "on_main"],
)
class TestLockDAReviewWithoutTheNote:
    """д. Read, printed and projected as before; stored and sent key for key."""

    def test_it_reads_with_no_note(self, stored: dict[str, Any]) -> None:
        assert ReviewStructureV1.model_validate(stored).unchanged_from_attempt is None

    def test_it_dumps_key_for_key(self, stored: dict[str, Any]) -> None:
        review = ReviewStructureV1.model_validate(stored)

        dumped = review.model_dump(mode="json")
        assert "unchanged_from_attempt" not in dumped
        assert set(dumped) == set(stored)

    def test_it_passes_curated_structure_and_the_webhook_sends_its_keys(
        self, stored: dict[str, Any]
    ) -> None:
        structure = curated_structure(stored)

        assert structure == ReviewStructureV1.model_validate(stored)
        student = MagicMock()
        student.external_id = "e-1"
        sent = build_reviewed_payload(_submission(stored), student).model_dump(
            mode="json"
        )
        assert set(sent["structure"]) == set(stored)

    def test_it_prints_no_note(self, stored: dict[str, Any]) -> None:
        markdown = assemble_review(ReviewStructureV1.model_validate(stored))

        assert phrases_for("ukr")["review.unchanged"].split("{")[0] not in markdown


def test_d_the_stored_review_prints_byte_for_byte_as_before() -> None:
    """д. The snapshot the assembler of main printed, generated before K3."""
    expected = (SNAPSHOTS / "review_v1_stored_ukr.md").read_text(encoding="utf-8")

    assert assemble_review(ReviewStructureV1.model_validate(STORED_BEFORE_TASK_10)) == (
        expected
    )


def test_d_on_main_the_text_review_dumps_exactly_what_was_stored() -> None:
    review = ReviewStructureV1.model_validate(_STORED_ON_MAIN)

    assert review.model_dump(mode="json") == _STORED_ON_MAIN


class TestTheNote:
    """The phrase opens the review, before the verdict, the number put in."""

    def test_it_opens_the_review_and_the_rest_is_the_review_without_it(self) -> None:
        without = assemble_review(_passed())

        repeated = assemble_review(_passed(unchanged_from_attempt=3))

        title, *rest = without.splitlines()
        assert repeated.splitlines() == [
            title,
            "",
            "Робота не змінилась із попередньої спроби (спроба 3), тому рецензію "
            "повторено без нової перевірки.",
            *rest,
        ]

    @pytest.mark.parametrize("language", _LANGUAGES)
    def test_in_every_language_the_number_is_put_in(self, language: str) -> None:
        phrase = phrases_for(language)["review.unchanged"]

        assembled = assemble_review(_passed(language, unchanged_from_attempt=7))

        assert assembled.splitlines()[2] == phrase.replace("{attempt}", "7")
        assert "{" not in assembled
        assert "}" not in assembled

    def test_it_is_stored_and_read_back(self) -> None:
        review = _passed(unchanged_from_attempt=2)

        dumped = review.model_dump(mode="json")

        assert dumped["unchanged_from_attempt"] == 2
        assert ReviewStructureV1.model_validate(dumped) == review
        assert curated_structure(dumped) == review

    def test_an_attempt_is_numbered_from_one(self) -> None:
        with pytest.raises(ValidationError):
            _passed(unchanged_from_attempt=0)

    def test_the_note_alone_is_no_review(self) -> None:
        with pytest.raises(ValidationError, match="says nothing to the student"):
            ReviewStructureV1(
                schema_version=REVIEW_SCHEMA_VERSION,
                language="ukr",
                unchanged_from_attempt=1,
            )
