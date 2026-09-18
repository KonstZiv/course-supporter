"""Contract-lock tests for the outbound webhook payload (sprint-mentor T5).

These freeze the 'reviewed' webhook field set and key constraints so any future
change to the caller-facing contract is a deliberate, test-breaking edit — per
decision 2.4.13 the Pydantic models ARE the canonical contract (there is no
parallel hand-written contract file).
"""

from __future__ import annotations

from datetime import UTC, datetime

from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.models.webhook import (
    ReviewSummary,
    WebhookFailedPayload,
    WebhookMismatchPayload,
    WebhookReviewedPayload,
)


class TestReviewSummaryContract:
    def test_field_set_is_locked(self) -> None:
        assert set(ReviewSummary.model_fields) == {
            "passed",
            "score",
            "correctness",
            "review_text",
            "response_language",
        }

    def test_score_bounded_0_100(self) -> None:
        score = ReviewSummary.model_json_schema()["properties"]["score"]
        assert score["minimum"] == 0
        assert score["maximum"] == 100

    def test_correctness_enum_locked(self) -> None:
        correctness = ReviewSummary.model_json_schema()["properties"]["correctness"]
        assert set(correctness["enum"]) == {
            "correct",
            "partially_correct",
            "incorrect",
        }

    def test_no_lifecycle_status_field(self) -> None:
        # 3a: outcome travels via passed + correctness, not a status string;
        # a lifecycle status field is a T7 concern once the vocabulary settles.
        assert "status" not in ReviewSummary.model_fields


class TestWebhookReviewedPayloadContract:
    def test_field_set_is_locked(self) -> None:
        assert set(WebhookReviewedPayload.model_fields) == {
            "event",
            "submission_id",
            "student_external_id",
            "review",
            # Optional versioned review structure: added by the ratified
            # decision of 2026-09-17 (mentor-rebuild task 04), which is the
            # only kind of change ``reviewed`` accepts. The rule beside the
            # models says so.
            "structure",
            "timestamp",
        }

    def test_structure_is_optional_and_null_today(self) -> None:
        """The addition costs an existing consumer nothing.

        A payload built the way every caller builds one today — without naming
        the new field — still validates, and carries it as null. No stage
        writes a structure yet, so null is what production sends.
        """
        payload = WebhookReviewedPayload(
            submission_id="s-1",
            student_external_id="e-1",
            review=ReviewSummary(
                passed=True,
                score=90,
                correctness="correct",
                review_text="# Review",
                response_language="uk",
            ),
            timestamp=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        )

        assert payload.structure is None
        assert payload.model_dump()["structure"] is None

    def test_structure_carries_its_own_version(self) -> None:
        """Versioned inside itself, not by a version field on the event: the
        event has no contract version and is not getting one here."""
        structure = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="ukr",
            progress="Третє завдання поспіль без зауважень до стилю.",
        )
        payload = WebhookReviewedPayload(
            submission_id="s-1",
            student_external_id="e-1",
            review=ReviewSummary(
                passed=True,
                score=90,
                correctness="correct",
                review_text="# Review",
                response_language="uk",
            ),
            structure=structure,
            timestamp=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        )

        assert payload.model_dump()["structure"]["schema_version"] == (
            REVIEW_SCHEMA_VERSION
        )
        assert "version" not in {f for f in WebhookReviewedPayload.model_fields}

    def test_event_is_reviewed_only(self) -> None:
        # 3b: T5 finalises only the 'reviewed' event; failed/mismatch land in T7
        # by widening this Literal, not by mutating the payload.
        assert WebhookReviewedPayload.model_fields["event"].default == "reviewed"
        event = WebhookReviewedPayload.model_json_schema()["properties"]["event"]
        assert event.get("const") == "reviewed" or event.get("enum") == ["reviewed"]


class TestWebhookMismatchPayloadContract:
    """T7 mismatch event — sanity gate rejection, no score (review never ran)."""

    def test_field_set_is_locked(self) -> None:
        assert set(WebhookMismatchPayload.model_fields) == {
            "event",
            "submission_id",
            "student_external_id",
            "reason",
            "timestamp",
        }

    def test_event_default_is_mismatch(self) -> None:
        assert WebhookMismatchPayload.model_fields["event"].default == "mismatch"

    def test_carries_no_review_or_score(self) -> None:
        # No review ran on a sanity-gated submission — the payload must not
        # imply one. Only the gate's reason travels.
        assert "review" not in WebhookMismatchPayload.model_fields
        assert "score" not in WebhookMismatchPayload.model_fields


class TestWebhookFailedPayloadContract:
    """T7 failed event — processing error, no review produced."""

    def test_field_set_is_locked(self) -> None:
        assert set(WebhookFailedPayload.model_fields) == {
            "event",
            "submission_id",
            "student_external_id",
            "reason",
            "timestamp",
        }

    def test_event_default_is_failed(self) -> None:
        assert WebhookFailedPayload.model_fields["event"].default == "failed"
