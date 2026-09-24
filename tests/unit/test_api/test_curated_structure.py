"""One projection decides what a stored review is (task 04, block B3).

``curated_structure`` is the only place that reads ``review_result`` and says
"this is a version-1 review structure". Both outward surfaces — the portal
detail and the ``reviewed`` webhook — go through it, so neither can grow its own
idea of what the column holds.

The column holds two different things. A pre-rebuild review put the Mentor's
layered working notes there, and those never leave the service. A version-1
review puts a structure there, and a structure is not a trace: every field of it
is written to be read by the student. The version key answers which, so nothing
here guesses from content.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from course_supporter.api.routes._portal_shared import (
    curated_structure,
    curated_verdict,
)
from course_supporter.api.schemas import PortalVerdict
from course_supporter.homework.webhook import build_reviewed_payload
from course_supporter.models import review_structure
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import ReviewStructureV1, Verdict

A_STRUCTURE = ReviewStructureV1(
    schema_version=REVIEW_SCHEMA_VERSION,
    language="ukr",
    verdict=Verdict(passed=True, why="Усе, про що просили, на місці."),
)

# What today's Mentor writes: a layered internal trace, no version key.
A_PRE_REBUILD_TRACE: dict[str, Any] = {
    "verdict": {"passed": True, "correctness": "correct"},
    "layers": {"node": "INTERNAL — secret layered judgment"},
    "denoised_score": 87,
}


def _submission(review_result: dict[str, Any] | None) -> MagicMock:
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.score = 87
    sub.review_markdown = "## Good work"
    sub.response_language = "uk"
    sub.review_result = review_result
    sub.created_at = datetime.now(UTC)
    return sub


class TestWhatItReads:
    def test_a_version_1_review_comes_back_whole(self) -> None:
        stored = A_STRUCTURE.model_dump()

        assert curated_structure(stored) == A_STRUCTURE

    def test_a_pre_rebuild_trace_is_not_a_structure(self) -> None:
        """The layered notes stay inside, refused BY THE VERSION KEY.

        The silence matters as much as the None. Without the version check the
        answer would still be None — the trace cannot validate as a structure —
        but it would arrive as a logged error, and every pre-rebuild review in
        production would write one on every read. Asserting only the None left
        that mutation green; this is the missing target.
        """
        with capture_logs() as logs:
            result = curated_structure(A_PRE_REBUILD_TRACE)

        assert result is None
        assert logs == []

    def test_nothing_stored_reads_as_nothing(self) -> None:
        assert curated_structure(None) is None
        assert curated_structure({}) is None

    def test_an_unknown_version_is_refused_rather_than_guessed(self) -> None:
        """A version this code cannot assemble is not read as one it can."""
        future = {**A_STRUCTURE.model_dump(), "schema_version": "2"}

        assert curated_structure(future) is None

    def test_a_row_claiming_version_1_that_does_not_validate_is_logged(self) -> None:
        """Our own bad data, not the student's problem.

        Raising would turn one bad row into a broken page; returning None
        without a word would make it unfindable. So: absent, and in the log.
        """
        broken = {"schema_version": REVIEW_SCHEMA_VERSION, "language": "not-a-code"}

        with capture_logs() as logs:
            result = curated_structure(broken)

        assert result is None
        assert [entry["event"] for entry in logs] == ["review_structure_invalid"]


class TestOneSourceForTwoSurfaces:
    def test_the_webhook_reads_the_structure_through_the_same_projection(
        self,
    ) -> None:
        student = MagicMock()
        student.external_id = "e-1"

        payload = build_reviewed_payload(_submission(A_STRUCTURE.model_dump()), student)

        assert payload.structure == A_STRUCTURE

    def test_the_webhook_sends_null_for_a_pre_rebuild_review(self) -> None:
        """Which is every review in production today."""
        student = MagicMock()
        student.external_id = "e-1"

        payload = build_reviewed_payload(_submission(A_PRE_REBUILD_TRACE), student)

        assert payload.structure is None

    def test_both_surfaces_agree_on_the_same_row(self) -> None:
        """The property the single projection exists for: a change of mind
        about what the column holds cannot reach one surface and miss the
        other, because there is no second place to change it."""
        from course_supporter.api.routes.portal_submissions import _to_detail

        submission = _submission(A_STRUCTURE.model_dump())
        submission.status = "completed"
        submission.original_filename = "solution.py"
        submission.snapshot_manifest = None
        submission.base_id = None
        student = MagicMock()
        student.external_id = "e-1"

        detail = _to_detail(submission)
        payload = build_reviewed_payload(submission, student)

        assert detail.structure == payload.structure == A_STRUCTURE


def _outcome(review_result: dict[str, Any] | None, score: int | None) -> tuple:
    """What each surface says about one row: the webhook's pair, the portal's."""
    submission = _submission(review_result)
    submission.score = score
    student = MagicMock()
    student.external_id = "e-1"
    review = build_reviewed_payload(submission, student).review
    return (review.passed, review.correctness), curated_verdict(
        review_result, score=score
    )


class TestAPreRebuildReviewReadsAsBefore:
    """Today's Mentor, on both surfaces, byte for byte (task 07, decision 14).

    The expected values are what the code gave before task 07 — frozen here,
    not recomputed — and the score is chosen so a version-1 reading would give
    a different answer: a trace is never read through the score.
    """

    @pytest.mark.parametrize(
        ("review_result", "score", "webhook", "portal"),
        [
            (
                A_PRE_REBUILD_TRACE,
                100,
                (True, "correct"),
                PortalVerdict(passed=True, correctness="correct"),
            ),
            (
                {"verdict": {"passed": False, "correctness": "incorrect"}},
                100,
                (False, "incorrect"),
                PortalVerdict(passed=False, correctness="incorrect"),
            ),
            (
                {"verdict": {"passed": True, "correctness": "partially_correct"}},
                0,
                (True, "partially_correct"),
                PortalVerdict(passed=True, correctness="partially_correct"),
            ),
            (
                {"verdict": {}},
                100,
                (False, "incorrect"),
                PortalVerdict(passed=False, correctness="incorrect"),
            ),
            ({"layers": {"node": "trace only"}}, 100, (False, "incorrect"), None),
            (None, 100, (False, "incorrect"), None),
        ],
        ids=["passed", "failed", "partial", "empty-verdict", "no-verdict", "no-review"],
    )
    def test_both_surfaces_give_what_they_gave(
        self,
        review_result: dict[str, Any] | None,
        score: int,
        webhook: tuple[bool, str],
        portal: PortalVerdict | None,
    ) -> None:
        assert _outcome(review_result, score) == (webhook, portal)


def _a_test_review(*, passed: bool | None, score: int) -> dict[str, Any]:
    """A version-1 test review as stored; ``passed=None``: no pass mark."""
    return ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language="ukr",
        verdict=None if passed is None else Verdict(passed=passed),
        test=review_structure.TestSection(
            score=score,
            questions=[review_structure.TestQuestionResult(number="1", correct=True)],
        ),
    ).model_dump()


class TestTheOutcomeOfATest:
    """Version 1 (task 07, decision 14): passed by the pass mark, correctness by
    the score — the same answer on the webhook and in the portal."""

    def test_a_passed_test_is_not_reported_incorrect(self) -> None:
        webhook, portal = _outcome(_a_test_review(passed=True, score=80), 80)

        assert webhook == (True, "partially_correct")
        assert portal == PortalVerdict(passed=True, correctness="partially_correct")

    def test_a_test_without_a_pass_mark_is_passed(self) -> None:
        """Nobody set a bar to fail, so a caller gating on it is not held back."""
        webhook, portal = _outcome(_a_test_review(passed=None, score=60), 60)

        assert webhook == (True, "partially_correct")
        assert portal == PortalVerdict(passed=True, correctness="partially_correct")

    def test_a_review_with_no_verdict_and_no_test_is_not_passed(self) -> None:
        """Only a test's missing verdict means "no bar"; any other stays False."""
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="ukr",
            progress="Третє завдання поспіль без зауважень до стилю.",
        ).model_dump()

        webhook, portal = _outcome(review, 90)

        assert webhook == (False, "partially_correct")
        assert portal == PortalVerdict(passed=False, correctness="partially_correct")

    def test_a_test_below_its_pass_mark_is_not_passed(self) -> None:
        webhook, portal = _outcome(_a_test_review(passed=False, score=40), 40)

        assert webhook == (False, "partially_correct")
        assert portal == PortalVerdict(passed=False, correctness="partially_correct")

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(100, "correct"), (0, "incorrect"), (1, "partially_correct")],
    )
    def test_correctness_follows_the_score(self, score: int, expected: str) -> None:
        webhook, portal = _outcome(_a_test_review(passed=None, score=score), score)

        assert webhook[1] == expected
        assert portal is not None
        assert portal.correctness == expected
