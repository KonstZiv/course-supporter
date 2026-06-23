"""Contract-lock tests for the outbound webhook payload (sprint-mentor T5).

These freeze the 'reviewed' webhook field set and key constraints so any future
change to the caller-facing contract is a deliberate, test-breaking edit — per
decision 2.4.13 the Pydantic models ARE the canonical contract (there is no
parallel hand-written contract file).
"""

from __future__ import annotations

from course_supporter.models.webhook import ReviewSummary, WebhookReviewedPayload


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
            "timestamp",
        }

    def test_event_is_reviewed_only(self) -> None:
        # 3b: T5 finalises only the 'reviewed' event; failed/mismatch land in T7
        # by widening this Literal, not by mutating the payload.
        assert WebhookReviewedPayload.model_fields["event"].default == "reviewed"
        event = WebhookReviewedPayload.model_json_schema()["properties"]["event"]
        assert event.get("const") == "reviewed" or event.get("enum") == ["reviewed"]
