"""The attempt number on the portal's answers and the reviewed webhook (task 13)."""

import uuid

from course_supporter.api.schemas import (
    PortalSubmissionDetail,
    PortalSubmissionListItem,
)
from course_supporter.homework.webhook import build_reviewed_payload
from course_supporter.models.webhook import WebhookReviewedPayload
from course_supporter.storage.orm import HomeworkSubmission, Student


def test_the_portal_list_item_has_an_optional_attempt_no() -> None:
    field = PortalSubmissionListItem.model_fields["attempt_no"]
    assert field.default is None


def test_the_portal_detail_has_an_optional_attempt_no() -> None:
    field = PortalSubmissionDetail.model_fields["attempt_no"]
    assert field.default is None


def test_the_reviewed_webhook_defaults_attempt_no_to_null() -> None:
    assert WebhookReviewedPayload.model_fields["attempt_no"].default is None


def test_the_reviewed_webhook_carries_the_attempt_number() -> None:
    submission = HomeworkSubmission(
        id=uuid.uuid4(),
        attempt_no=2,
        review_result=None,
        score=80,
        review_markdown="",
        response_language="uk",
    )
    student = Student(external_id="student-1")
    payload = build_reviewed_payload(submission, student)
    assert payload.attempt_no == 2
    assert payload.model_dump(mode="json")["attempt_no"] == 2


def test_an_uncounted_submission_sends_null() -> None:
    submission = HomeworkSubmission(
        id=uuid.uuid4(),
        attempt_no=None,
        review_result=None,
        score=0,
        review_markdown="",
        response_language="uk",
    )
    student = Student(external_id="student-1")
    payload = build_reviewed_payload(submission, student)
    assert payload.model_dump(mode="json")["attempt_no"] is None
