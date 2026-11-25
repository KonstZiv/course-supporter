"""The boundary where a stored explanation is read.

Purpose:
    A stored explanation that does not read is our own bad data, and the error
    pydantic raises for it quotes the Mentor's words about the student's work.
    This module is the one place that turns such an error into a
    :class:`ResultNotBuiltError` without values. It is read through here by the
    builder (the explanation of this attempt) and by
    :func:`~course_supporter.homework.earlier_review.read_earlier_review` (the
    explanation of the earlier one).

Interface:
    :func:`read_stored_explanation` — reads one stored explanation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from course_supporter.homework.result_builders import PATH_FAILED, ResultNotBuiltError
from course_supporter.homework.verdict_explanation import ExplanationAnswer
from course_supporter.models.stored_read import problems_without_values, read_stored

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)

__all__ = ["read_stored_explanation"]


def read_stored_explanation(
    body: Mapping[str, Any], *, submission_id: uuid.UUID, whose: str
) -> ExplanationAnswer:
    """The stored explanation of a submission, read at the builder's boundary.

    :func:`~course_supporter.models.stored_read.read_stored`, with one thing
    more: a row that does not read is our own bad data, and the error pydantic
    raises for it holds the value of every field that failed — the Mentor's
    words about the student's work, quoted. That error does not leave here. It
    becomes a :class:`ResultNotBuiltError` with the code the submission failed
    with before (:data:`PATH_FAILED`: a failure on our side), a message without
    values, and ``from None`` so that no traceback carries the original; the
    place and the kind of each error go to the log in one line.

    Args:
        body: The stored explanation's body.
        submission_id: The submission whose explanation it is, for the log.
        whose: ``"explanation"`` or ``"earlier explanation"``: which of the
            two reads this is, for the log and the message.

    Raises:
        ResultNotBuiltError: ``path_failed`` — the row does not read.
    """
    try:
        return read_stored(ExplanationAnswer, body, submission_id=submission_id)
    except ValidationError as error:
        logger.error(
            "stored_explanation_unreadable",
            submission_id=str(submission_id),
            whose=whose,
            problems=problems_without_values(error),
        )
        msg = f"the stored {whose} does not read ({error.error_count()} errors)"
        raise ResultNotBuiltError(PATH_FAILED, msg) from None
