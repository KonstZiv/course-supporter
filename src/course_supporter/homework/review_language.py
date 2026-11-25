"""The language a review will be written in: one answer for all who ask.

Purpose:
    Three places must agree on the language of one submission's review, or the
    product owner's rule of 2026-11-23 (V-6: a copy of an earlier review only in
    the same language) breaks at the seam between them: the key of the copy
    (:mod:`~course_supporter.homework.attempt_dedup`), the price shown before the
    work is sent (``billing/quotes.py``) and the explanation stage that writes
    the review (:mod:`~course_supporter.homework.review_explanation`). All three
    read the answer here.

The rule:
    The doors' answer
    (:func:`~course_supporter.homework.path_runner.door_languages`: the language
    named on the submission, else the student's stored one, else the task's);
    and when the doors give none, the language of the course's root, which the
    explanation stage has always fallen back to. A review is written in a
    language even when no one named it, so the key and the price cannot take
    "no language" for "no copy possible" while the review itself has one.

Interface:
    :func:`review_language_of` — a submission, stored or not (a probe built of
    the student, the task and the form's language prices the work before it is
    sent): the language its review will be written in.
    :func:`language_for_review` — the same rule for a caller that already holds
    the doors' answer (the explanation stage gets it from the path).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from course_supporter.language import resolve_review_language
from course_supporter.storage.orm import CourseNode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import HomeworkSubmission

__all__ = ["language_for_review", "review_language_of"]


async def language_for_review(
    session: AsyncSession,
    *,
    course_node_id: uuid.UUID,
    doors_language: str | None,
) -> str | None:
    """The review's language given the doors' answer, ISO 639-3, or ``None``.

    ``doors_language`` as :func:`~course_supporter.homework.path_runner.door_languages`
    gives it. Without it, the language of the course root ``course_node_id``,
    read only then. ``None`` when neither gives a language (a root without one
    is refused by the database and by every write of it, so this is a defect).
    """
    root_language: str | None = None
    if doors_language is None:
        root = await session.get(CourseNode, course_node_id)
        root_language = root.default_language if root is not None else None
    return resolve_review_language(
        explicit=doors_language, preferred=None, course=root_language
    ).code


async def review_language_of(
    session: AsyncSession, submission: HomeworkSubmission
) -> str | None:
    """The language the review of ``submission`` will be written in.

    Reads, never writes. ``submission`` needs ``student_id``,
    ``authored_document_id``, ``course_node_id`` (the course root) and
    ``response_language``; it need not be stored. ``None`` only when no source
    and no course root gives a language.
    """
    # Imported here: the runner imports the dedup, which reads this module.
    from course_supporter.homework.path_runner import door_languages

    doors = await door_languages(session, submission)
    return await language_for_review(
        session,
        course_node_id=submission.course_node_id,
        doors_language=doors.review,
    )
