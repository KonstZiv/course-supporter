"""The one place a touch is decided and written (mentor-rebuild task 05).

Two doors lead here — the student's own portal session and the caller's channel
with a tenant key — and they differ in exactly one thing: how they find the
submission and which student they name. Everything after that is here, so the
two cannot answer the same situation differently. An entry point that wants to
add a check of its own is a signal that the check belongs in this module.

The order of the refusals is load-bearing, not stylistic:

1. no submission, a submission that is not this student's, or one that is
   soft-deleted — a generic "not found", the SAME answer all three get on the
   read path, and the same one an unknown id gets;
2. a submission with no review to speak about — a 409 with a reason code.

Checking the review first would answer 409 for a submission that is not the
student's, and 409 says "this exists" (invariant 4 of the task package). The
refusal for a review that is not written is only ever given to the owner of a
live submission, who can see that state anyway.

The access checks are made HERE even though both entry points already filter on
them. The tenant is taken from the submission for the same reason: a value the
core is told is a value that can disagree with the object it describes, and a
door whose filter is one day rewritten would then write one student's answer
under another student's name, silently. The entry points keep their filters —
they answer the question "which door is this" — and the invariant holds in the
core regardless of how they answer it.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.orm import (
    HomeworkStatus,
    HomeworkSubmission,
    StudentFeedback,
)

SUBMISSION_NOT_FOUND: Final = "Submission not found."
"""The one answer to every access failure, on both entry points.

Word for word the answer of ``GET /portal/submissions/{id}`` — a touch must not
become the surface that tells a student which submissions exist.
"""

NO_REVIEW_CODE: Final = "no_review"
"""Reason code of the 409 refusal; the portal keeps the sentence for it.

Without "yet": the refusal also covers a submission that ENDED without a
review — rejected at the door, refused as off-task, failed — where "yet" would
be false. A code is an external contract, and renaming one after a caller has
integrated with it is a breaking change, so it is chosen for the whole set of
cases it answers rather than for the commonest one.
"""

NO_REVIEW_DETAILS: Final = (
    "This submission has no review to answer about. If it is still being "
    "checked, the answer becomes possible once the review is written; if it "
    "ended without one, there is nothing the answer would be about."
)

_REVIEWED_STATUSES: Final = frozenset(
    {HomeworkStatus.COMPLETED.value, HomeworkStatus.DELIVERED.value}
)
"""The two milestones in which a review was actually written.

The same pair ``_PRESENTATION_STATE`` maps to the ``reviewed`` state and
``HomeworkRepository.has_reviewed_revision`` calls "already had a review": one
definition of a reviewed submission, read from three places.
"""


def _not_found() -> HTTPException:
    """The one refusal every access failure gets, built in one place."""
    return HTTPException(status_code=404, detail=SUBMISSION_NOT_FOUND)


def review_is_readable(submission: HomeworkSubmission | None) -> bool:
    """Whether this submission carries a review a student could have read.

    The status alone is not enough. A ``completed`` submission can still hold
    an empty ``review_markdown`` — the portal renders "Рецензію ще не
    сформовано" for exactly that row — and a touch on it would be an answer
    about nothing. One definition serves the server and the buttons: the
    student answers about what they read.

    ``None`` is not readable, which is what lets the caller ask the two
    questions in either order without the second one crashing on it. The order
    it should ask them in is the module docstring's subject.
    """
    if submission is None:
        return False
    if submission.status not in _REVIEWED_STATUSES:
        return False
    return bool((submission.review_markdown or "").strip())


async def touch_review(
    session: AsyncSession,
    *,
    submission: HomeworkSubmission | None,
    student_id: uuid.UUID,
    value: FeedbackValue,
) -> StudentFeedback:
    """Record this student's answer about this submission's review.

    Args:
        session: Database session; the caller commits.
        submission: The submission, as the entry point resolved it under its
            own access filter — ``None`` when anything about that resolution
            failed. The core does not repeat the RESOLUTION (only the entry
            point knows which door was used) but does re-check what it means:
            the row must be alive and must belong to ``student_id``.
        student_id: The student the entry point authenticated. Checked against
            the submission's owner rather than trusted.
        value: The answer.

    Returns:
        The stored feedback row — inserted, or replacing this student's
        previous answer about the same review.

    Raises:
        HTTPException: 404 with :data:`SUBMISSION_NOT_FOUND` when there is no
            submission to answer about, when it is not this student's, or when
            it is soft-deleted; 409 with :data:`NO_REVIEW_CODE` when there is
            one but it holds no review.
    """
    if submission is None:
        raise _not_found()
    if submission.deleted_at is not None:
        raise _not_found()
    if submission.student_id != student_id:
        raise _not_found()
    if not review_is_readable(submission):
        raise HTTPException(
            status_code=409,
            detail={"code": NO_REVIEW_CODE, "details": NO_REVIEW_DETAILS},
        )

    # Owner and tenant are read off the submission, never off the request
    # beside it. Both were just checked against it, so the values are the same
    # today; taking them from the object is what keeps them the same tomorrow,
    # when a door's filter is rewritten by someone who has not read this file.
    return await FeedbackRepository(session).record(
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
        target_kind=FeedbackTargetKind.REVIEW,
        target_id=submission.id,
        kind=FeedbackKind.TOUCH,
        value=value,
    )
