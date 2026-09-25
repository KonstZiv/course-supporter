"""What the author's routes share (mentor-rebuild tasks 06, 07b).

The answer to a job collision: the routes of a test's answer key and the
routes of a test written in the system meet the same one slot the database
allows a task, and answer it from one place, so the two cannot drift.
"""

from __future__ import annotations

from fastapi import HTTPException

from course_supporter.homework.reference_service import GenerationInProgressError


def generation_in_progress(exc: GenerationInProgressError) -> HTTPException:
    """Turn a job collision into the 409 the author reads (task 07, decision 9).

    409, not 422: nothing is wrong with the key, the draft or the task. Another
    job of this task holds the one slot the database allows; the request is not
    wrong, only early. Nothing the request wrote stands — not a key, not a
    version: the refused insert voided the transaction, and the route rolls the
    session back before answering (``GenerationInProgressError`` says why both).
    The answer says so.
    """
    return HTTPException(
        status_code=409,
        detail={
            "code": exc.code,
            "details": (
                "another job of this task is still running — its explanations "
                "being written, or the task itself being processed; nothing was "
                "saved, so send the request again once it finishes"
            ),
        },
    )
