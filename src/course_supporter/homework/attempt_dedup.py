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
    * the evaluator that wrote its review is the type's evaluator today: the
      ``evaluator_version`` of its receipt equals the type's version as the
      file stands now
      (:func:`~course_supporter.homework.evaluator_version.evaluator_version`
      — one version per type, whichever path a review walked). An edited
      prompt or another first model on any path of the type, other rules of
      the code — the review is written anew;
    * for a text task (``short_task``, ``task``): the criteria list its
      verdicts were judged against is the list in force for the task now.

    Anything else — no fingerprint (the ledger did not write one), no version,
    no evaluator's version (every review written before it was recorded),
    a type with no version (not on the new path, or no model stage), no list
    in force, any key different — is an ordinary review.

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
    number, in its attempt receipt, beside the ``evaluator_version`` of that
    review: the copy was written by the source's evaluator. The body then
    finishes it as it finishes any reviewed revision: ``reviewing``,
    ``completed``, the webhook, ``delivered``; the portal reads the stored
    review as it reads any.

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
    The reads before the decision — the receipt and the lookup — write
    nothing and run in a savepoint (as the attempt ledger writes in one). If
    one raises, the savepoint is rolled back, so the session the body goes on
    with is not left in an aborted transaction; the error is logged as a
    warning with the submission's id and the work is reviewed as usual — a
    missed repeat costs a review, a broken one would cost the student theirs.
    The copy and the receipt are written after the decision, as any write of
    the body is: if they fail, the submission fails the body's way.

One rule of the same work (product owner's decision, 2026-11-12):
    For a type this dedup serves, nothing before it turns the same work away.
    The entry's older check — the same file, by the hash of its raw bytes,
    answered "already submitted" with no new submission
    (``submission_core``, ``homework_duplicate_detected``) — is not applied
    to it: a file identical byte for byte is a new submission, reaches the
    doors and meets this module — the copy, or an ordinary review once the
    evaluator changed. A type on today's Mentor (``project``) and a type with
    no dedup (``test``) keep the entry's check as it was.
    :func:`same_work_reaches_the_new_path` decides, from the submission-path
    file and the attempt adapter: one place, no list of types.

Interface:
    :func:`same_work_reaches_the_new_path` — whether a type's same work goes
    on to this dedup rather than being turned away at the entry.
    :func:`find_unchanged_attempt` — the earlier attempt whose review to give
    again, or ``None``.
    :func:`repeat_unchanged_review` — what the body calls: the lookup, the
    copy and the receipt; returns the number repeated, or ``None``.
    :data:`DEDUP_RECEIPT_KEY` — the receipt's key.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import ValidationError
from sqlalchemy import select

from course_supporter.homework.attempts import attempt_snapshotter_for
from course_supporter.homework.criteria_list_service import load_in_force
from course_supporter.homework.evaluator_version import (
    EVALUATOR_VERSION_RECEIPT_KEY,
    evaluator_version,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    get_path_config,
)
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
    "same_work_reaches_the_new_path",
]

logger = structlog.get_logger(__name__)

DEDUP_RECEIPT_KEY: Final = "dedup_of"
"""The receipt's key for the number of the attempt whose review was repeated."""

_JUDGED_AGAINST_A_LIST: Final = frozenset(
    {AssignmentType.SHORT_TASK.value, AssignmentType.TASK.value}
)
"""The types whose review is judged against a criteria list (task 09b)."""


def same_work_reaches_the_new_path(
    task_type: str, config: PathConfig | None = None
) -> bool:
    """Does the same work of this type go on to this dedup? (the module's rule)

    True for a type the new path serves (``served_by``) whose attempt adapter
    deduplicates (``dedup_enabled``) — ``task`` and ``short_task`` today: the
    entry does not turn a byte-identical file away. False for every other
    type — on today's Mentor, or never deduplicated — whose entry keeps its
    check by the raw file's hash. ``config`` defaults to the cached
    submission-path file, the one the body serves by.
    """
    cfg = config if config is not None else get_path_config()
    declared = cfg.task_types.get(AssignmentType(task_type))
    return (
        declared is not None
        and declared.served_by is ServedBy.NEW_PATH
        and attempt_snapshotter_for(task_type).dedup_enabled
    )


@dataclass(frozen=True, slots=True)
class UnchangedAttempt:
    """The earlier attempt of the same work whose review is given again.

    Attributes:
        submission_id: The earlier submission.
        attempt_no: Its number — what the copy and the receipt name.
        review: Its review, read as a version-1 structure.
        score: Its score.
        evaluator_version: The version of the evaluator that wrote the review.
    """

    submission_id: uuid.UUID
    attempt_no: int
    review: ReviewStructureV1
    score: int | None
    evaluator_version: str


async def repeat_unchanged_review(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    config: PathConfig,
) -> int | None:
    """Give the submission its earlier attempt's review, if the work is the same.

    Call right after the attempt ledger, before any stage. ``config`` is the
    submission-path file the submission is served by: the type's evaluator is
    read from it. On a match the copy and the receipt are flushed into
    ``session`` for the caller to commit with its status; nothing else is
    written. Returns the number of the attempt repeated — also when an earlier
    run of this submission already repeated it — or ``None`` for an ordinary
    review.
    """
    looked_up = await _in_a_savepoint(
        session, submission_id, partial(_look_up, config=config)
    )
    if looked_up is None:
        return None
    receipt, source = looked_up
    if receipt is None:
        # The ledger wrote nothing (it failed, logged): no fingerprint to match.
        return None
    if DEDUP_RECEIPT_KEY in receipt:
        return int(receipt[DEDUP_RECEIPT_KEY])
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
        submission_id,
        dedup_of=source.attempt_no,
        evaluator_version=source.evaluator_version,
        receipt=receipt,
    )
    logger.info(
        "attempt_review_repeated",
        submission_id=str(submission_id),
        dedup_of=source.attempt_no,
        source_submission_id=str(source.submission_id),
        evaluator_version=source.evaluator_version,
    )
    return source.attempt_no


async def find_unchanged_attempt(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    config: PathConfig,
) -> UnchangedAttempt | None:
    """The earlier attempt of the same work, judged alike, or ``None``.

    The rule is the module's; ``config`` as on :func:`repeat_unchanged_review`.
    Never raises: the lookup runs in a savepoint, and an error is logged and
    read as no match (:func:`_in_a_savepoint`).
    """
    return await _in_a_savepoint(session, submission_id, partial(_find, config=config))


async def _in_a_savepoint[T](
    session: AsyncSession,
    submission_id: uuid.UUID,
    read: Callable[[AsyncSession, uuid.UUID], Awaitable[T]],
) -> T | None:
    """Run a read of the dedup in a savepoint; ``None`` when it failed.

    Everything the dedup asks before it decides — the receipt, the type's
    evaluator, the lookup — goes through here. A query that fails aborts the
    database's transaction; rolled back to the savepoint, the caller's session
    stays usable, and the work is reviewed as usual: the funds port, the
    stages and the review are written through that same session. The error is logged as
    a warning with the submission's id, as the attempt ledger logs its own.
    """
    try:
        async with session.begin_nested():
            return await read(session, submission_id)
    except Exception:
        logger.warning(
            "attempt_dedup_lookup_failed",
            submission_id=str(submission_id),
            exc_info=True,
        )
        return None


async def _look_up(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    config: PathConfig,
) -> tuple[dict[str, Any] | None, UnchangedAttempt | None]:
    """The submission's receipt, and the attempt to repeat when one is to be found.

    No lookup when there is no receipt (no fingerprint to match), nor when the
    receipt already names the attempt repeated (a run re-entered after its
    copy was committed).
    """
    receipt = await session.scalar(
        select(HomeworkSubmission.attempt_receipt).where(
            HomeworkSubmission.id == submission_id
        )
    )
    if receipt is None or DEDUP_RECEIPT_KEY in receipt:
        return receipt, None
    return receipt, await _find(session, submission_id, config=config)


async def _find(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    config: PathConfig,
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
    version = evaluator_version(AssignmentType(row.task_type), config)
    if version is None:
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
        evaluator_version=version,
    )
    if source is None or source.attempt_no is None:
        return None
    review = _version_1_review(source.review_result)
    if review is None:
        logger.warning(
            "attempt_dedup_review_unreadable",
            submission_id=str(submission_id),
            source_submission_id=str(source.id),
        )
        return None
    return UnchangedAttempt(
        submission_id=source.id,
        attempt_no=source.attempt_no,
        review=review,
        score=source.score,
        evaluator_version=_evaluator_version_of(source.attempt_receipt),
    )


def _evaluator_version_of(receipt: dict[str, Any] | None) -> str:
    """The evaluator's version a matched source's receipt names (the lookup's key)."""
    if receipt is None or not isinstance(
        version := receipt.get(EVALUATOR_VERSION_RECEIPT_KEY), str
    ):
        msg = "a source of a copy names no evaluator's version"
        raise ValueError(msg)
    return version


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
