"""One projection of a stored answer, for the two doors that serve one.

Both entry points of a touch — the portal session and the caller's channel —
answer with the same shape, and the submission detail serves that shape again
on the next read. Three call sites, one function: a projection copied into two
route modules is a projection that will one day differ between them, which is
the same reason the decision itself lives in a single core.
"""

from __future__ import annotations

from course_supporter.api.schemas import FeedbackTouch
from course_supporter.feedback_kinds import FeedbackKind, FeedbackValue
from course_supporter.storage.orm import StudentFeedback


def to_touch(row: StudentFeedback) -> FeedbackTouch:
    """The stored row as the answer served back to a caller."""
    return FeedbackTouch(
        kind=FeedbackKind(row.kind),
        value=FeedbackValue(row.value),
        updated_at=row.updated_at,
    )
