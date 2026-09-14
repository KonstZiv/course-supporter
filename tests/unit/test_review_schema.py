"""Review schema version — required on the new form, absent means pre-rebuild."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from course_supporter.models.mentor_review import (
    HistoryReconciliation,
    Layer,
    ReviewResult,
    Verdict,
)
from course_supporter.models.review_schema import (
    PRE_REBUILD_SCHEMA,
    REVIEW_SCHEMA_VERSION,
    VersionedReview,
    review_schema_version,
)


class _BodyForTest(VersionedReview):
    """Stand-in for task 04's review body: the base's rule must be inherited."""

    summary: str


class TestVersionedReview:
    def test_a_new_form_review_cannot_be_assembled_without_a_version(self) -> None:
        with pytest.raises(ValidationError, match="schema_version"):
            _BodyForTest(summary="looks fine")  # type: ignore[call-arg]

    def test_assembled_with_the_current_version(self) -> None:
        review = _BodyForTest(schema_version=REVIEW_SCHEMA_VERSION, summary="ok")
        assert review.model_dump()["schema_version"] == REVIEW_SCHEMA_VERSION

    def test_an_unknown_version_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="schema_version"):
            _BodyForTest(schema_version="999", summary="ok")  # type: ignore[arg-type]

    def test_the_version_travels_with_the_stored_structure(self) -> None:
        stored = _BodyForTest(
            schema_version=REVIEW_SCHEMA_VERSION, summary="ok"
        ).model_dump(mode="json")
        assert review_schema_version(stored) == REVIEW_SCHEMA_VERSION


class TestReviewSchemaVersionReader:
    def test_todays_review_result_reads_as_pre_rebuild(self) -> None:
        """The real pre-rebuild shape (today's Mentor), not a hand-made dict."""
        layers = [
            Layer(layer=name, weight=0.3, score=70, strengths=[], weaknesses=[])
            for name in ("node", "course", "industry")
        ]
        stored = ReviewResult(
            layers=layers,
            aggregate_score=70,
            history_reconciliation=HistoryReconciliation(
                recidivism=[], corrections=[], denoise_delta=0, denoised_score=70
            ),
            score_signals=[],
            verdict=Verdict(passed=True, correctness="partially_correct"),
        ).model_dump(mode="json")

        assert review_schema_version(stored) == PRE_REBUILD_SCHEMA

    @pytest.mark.parametrize("junk", [1, None, ["1"]], ids=["int", "null", "list"])
    def test_a_present_but_malformed_version_is_not_guessed(self, junk: object) -> None:
        with pytest.raises(ValueError, match="schema_version must be a string"):
            review_schema_version({"schema_version": junk})
