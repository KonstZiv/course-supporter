"""The same work sent again, answered with the review it already got (task 13, K3).

Purpose:
    A student who sends the same work at the same task again gets the review
    of their earlier attempt, and nothing is paid for it: no model is called,
    the funds port is not asked (billing's decision: such a submission costs
    nothing and blocks no reserve), and the call register gets no row. Only the
    new path asks (``homework/path_runner.py``), right after the attempt ledger
    wrote the submission's fingerprint and before the first stage — the safety
    stage included, because it is a model call too. Today's Mentor
    (``api/tasks.py``) does not ask.

The same work, judged alike:
    Among the student's earlier ATTEMPTS at the same task — live submissions
    that got a review (``homework_repository.ATTEMPT_STATUSES``, the ledger's
    own notion: a submission refused for safety, judged off-task or failed is
    not one) — the latest one for which all of these hold:

    * its fingerprint (K1, stored by the ledger) is the submission's;
    * its ``task_version`` is the submission's;
    * for a text task (``short_task``, ``task``): the criteria list its
      verdicts were judged against is the list in force for the task now.

    Anything else — no fingerprint (the ledger did not write one), no version,
    no list in force, any key different — is an ordinary review.

The version of a criteria list:
    Nothing new: the ADDRESS every verdict row carries — ``criteria_layer`` +
    ``criteria_source_id`` (task 08, decision 21; ``judged_list.py`` reads a
    list back by it). A list's row is written once: an author's edit is a new
    override row, a recomposition releases the model's row and claims a new
    one. So one address is one version of the list, and the list in force now
    (:func:`~course_supporter.homework.criteria_list_service.load_in_force`,
    no model call) is compared by its address. An attempt with no verdict rows
    was judged against no list — a review by today's Mentor, or a review that
    was itself repeated — and is the source of no text task's copy; the copy is
    taken from the attempt that was judged.

    A project has no list on the new path: its key is the fingerprint and the
    version alone. Projects are served by today's Mentor; the branch is there
    for the day they move.

What the submission gets:
    The earlier attempt's review — its structure, with
    ``unchanged_from_attempt`` set to that attempt's number, the markdown
    assembled from it by the one assembler (the earlier text, opened by the
    phrase ``review.unchanged``), and its score — and ``dedup_of``, the same
    number, in its attempt receipt. The body then finishes it as it finishes
    any reviewed revision: ``reviewing``, ``completed``, the webhook,
    ``delivered``; the portal reads the stored review as it reads any.

Once:
    The copy and the receipt are flushed into the body's transaction, which
    commits them with ``reviewing``. A run re-entered after that commit — the
    worker died before the delivery — finds ``dedup_of`` in the receipt and is
    told the number without anything being copied again; the body goes on to
    the delivery. A revision continued from a checkpoint is past this point:
    the body asks only on a run with no checkpoint, before the funds port was
    ever asked for it.

Not repeated:
    * a type whose adapter answers ``dedup_enabled`` ``False`` — a test, every
      attempt of which is scored anew (task 07);
    * an earlier review that is not a version-1 structure (logged): only a
      review the assembler can write out again can be given again.

Failure:
    The lookup reads and writes nothing; if it raises, the error is logged and
    the work is reviewed as usual — a missed repeat costs a review, a broken
    one would cost the student theirs.

Interface:
    :func:`find_unchanged_attempt` — the earlier attempt whose review to give
    again, or ``None``.
    :func:`repeat_unchanged_review` — what the body calls: the lookup, the
    copy and the receipt; returns the number repeated, or ``None``.
    :data:`DEDUP_RECEIPT_KEY` — the receipt's key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import ValidationError
from sqlalchemy import select

from course_supporter.homework.attempts import attempt_snapshotter_for
from course_supporter.homework.criteria_list_service import load_in_force
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.models.review_schema import (
    REVIEW_SCHEMA_VERSION,
    review_schema_version,
)
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument, HomeworkSubmission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DEDUP_RECEIPT_KEY",
    "UnchangedAttempt",
    "find_unchanged_attempt",
    "repeat_unchanged_review",
]

logger = structlog.get_logger(__name__)

DEDUP_RECEIPT_KEY: Final = "dedup_of"
"""The receipt's key for the number of the attempt whose review was repeated."""

_JUDGED_AGAINST_A_LIST: Final = frozenset(
    {AssignmentType.SHORT_TASK.value, AssignmentType.TASK.value}
)
"""The types whose review is judged against a criteria list (task 09b)."""


@dataclass(frozen=True, slots=True)
class UnchangedAttempt:
    """The earlier attempt of the same work whose review is given again.

    Attributes:
        submission_id: The earlier submission.
        attempt_no: Its number — what the copy and the receipt name.
        review: Its review, read as a version-1 structure.
        score: Its score.
    """

    submission_id: uuid.UUID
    attempt_no: int
    review: ReviewStructureV1
    score: int | None


async def repeat_unchanged_review(
    session: AsyncSession, submission_id: uuid.UUID
) -> int | None:
    """Give the submission its earlier attempt's review, if the work is the same.

    Call right after the attempt ledger, before any stage. On a match the copy
    and the receipt are flushed into ``session`` for the caller to commit with
    its status; nothing else is written. Returns the number of the attempt
    repeated — also when an earlier run of this submission already repeated it
    — or ``None`` for an ordinary review.
    """
    receipt = await session.scalar(
        select(HomeworkSubmission.attempt_receipt).where(
            HomeworkSubmission.id == submission_id
        )
    )
    if receipt is None:
        # The ledger wrote nothing (it failed, logged): no fingerprint to match.
        return None
    if DEDUP_RECEIPT_KEY in receipt:
        return int(receipt[DEDUP_RECEIPT_KEY])
    source = await find_unchanged_attempt(session, submission_id)
    if source is None:
        return None

    review = source.review.model_copy(
        update={"unchanged_from_attempt": source.attempt_no}
    )
    hw_repo = HomeworkRepository(session)
    await hw_repo.store_review_result(
        submission_id,
        result=review.model_dump(mode="json"),
        review_markdown=assemble_review(review),
        score=source.score,
    )
    await hw_repo.store_dedup_of(
        submission_id, dedup_of=source.attempt_no, receipt=receipt
    )
    logger.info(
        "attempt_review_repeated",
        submission_id=str(submission_id),
        dedup_of=source.attempt_no,
        source_submission_id=str(source.submission_id),
    )
    return source.attempt_no


async def find_unchanged_attempt(
    session: AsyncSession, submission_id: uuid.UUID
) -> UnchangedAttempt | None:
    """The earlier attempt of the same work, judged alike, or ``None``.

    The rule is the module's. Never raises: an error is logged and read as no
    match, so the work is reviewed as usual.
    """
    log = logger.bind(submission_id=str(submission_id))
    try:
        return await _find(session, submission_id, log)
    except Exception:
        log.exception("attempt_dedup_lookup_failed")
        return None


async def _find(
    session: AsyncSession, submission_id: uuid.UUID, log: Any
) -> UnchangedAttempt | None:
    row = (
        await session.execute(
            select(
                HomeworkSubmission.tenant_id,
                HomeworkSubmission.student_id,
                HomeworkSubmission.authored_document_id,
                HomeworkSubmission.fingerprint,
                HomeworkSubmission.task_version,
                AuthoredDocument.task_type,
            )
            .join(
                AuthoredDocument,
                AuthoredDocument.id == HomeworkSubmission.authored_document_id,
            )
            .where(HomeworkSubmission.id == submission_id)
        )
    ).one_or_none()
    if (
        row is None
        or row.fingerprint is None
        or row.task_version is None
        or row.task_type is None
        or not attempt_snapshotter_for(row.task_type).dedup_enabled
    ):
        return None

    criteria_list: tuple[str, uuid.UUID] | None = None
    if row.task_type in _JUDGED_AGAINST_A_LIST:
        in_force = await load_in_force(session, row.authored_document_id)
        if in_force is None:
            return None
        criteria_list = (in_force.layer, in_force.source_id)

    source = await HomeworkRepository(session).latest_unchanged_attempt(
        tenant_id=row.tenant_id,
        student_id=row.student_id,
        authored_document_id=row.authored_document_id,
        excluding=submission_id,
        fingerprint=row.fingerprint,
        task_version=row.task_version,
        criteria_list=criteria_list,
    )
    if source is None or source.attempt_no is None:
        return None
    review = _version_1_review(source.review_result)
    if review is None:
        log.warning(
            "attempt_dedup_review_unreadable",
            source_submission_id=str(source.id),
        )
        return None
    return UnchangedAttempt(
        submission_id=source.id,
        attempt_no=source.attempt_no,
        review=review,
        score=source.score,
    )


def _version_1_review(review_result: dict[str, Any] | None) -> ReviewStructureV1 | None:
    """The stored review as a version-1 structure, or ``None`` when it is not one."""
    if not review_result or review_schema_version(review_result) != (
        REVIEW_SCHEMA_VERSION
    ):
        return None
    try:
        return ReviewStructureV1.model_validate(review_result)
    except ValidationError:
        return None
