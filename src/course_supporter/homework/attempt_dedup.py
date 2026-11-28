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
    * for a type judged against a criteria list — a text task (``short_task``,
      ``task``) or a project: the criteria list its verdicts were judged
      against is the list in force for the task now;
    * the language of its stored review (``ReviewStructureV1.language``, ISO
      639-3) is the language this submission's review is to be written in —
      the product owner's decision of 2026-11-23 (V-6, variant 1): a copy is
      given in the same language only; the same work asked for in another
      language is a new, full, paid review like any other. The submission's
      language is the one its review will be written in
      (:func:`~course_supporter.homework.review_language.review_language_of`:
      the doors' answer — the language named on the submission, else the
      student's stored one, else the task's — and without it the course root's,
      as the review itself is; never the raw ``response_language``, which may
      be empty). The price shown before the work is sent asks the same
      function, so the price and the copy agree. It is a condition of the
      choice, not a check on the latest attempt: of attempt 1 in Ukrainian,
      attempt 2 the same work in English (a new review) and attempt 3 the same
      work in Ukrainian, attempt 3 is given the review of attempt 1. A
      submission to which not even the course root gives a language has
      nothing to compare, and is an ordinary review.

    Anything else — no fingerprint (the ledger did not write one), no version,
    no evaluator's version (every review written before it was recorded),
    a type with no version (not on the new path, or no model stage), no list
    in force, no language, any key different — is an ordinary review.

    And the same work is no copy while the student has a reply about the work
    to answer (task 12a, K3;
    :func:`~course_supporter.homework.remark_comments.pending_replies` — one
    definition with the submission's state and the reply screen): the copy
    would answer nothing they wrote. When EVERY reply to answer was judged by
    the reply screen as off the topic or hostile, the work is the copy all
    the same — there is nothing in the replies for a paid check to read —
    and each reply gets the phrasebook's answer without a model: a hostile
    one ``reply.hostile.fallback``, one off the topic ``reply.off_topic.wrap_up``
    with the course's name (the product owner's decision of 2026-11-16, its
    fourth item; task 12a, K4;
    :func:`~course_supporter.homework.reply_source.answer_without_the_model`).
    One reply about the work, or one the screen could not judge, and the work
    is reviewed in full on ``repeat_with_replies``. A type that takes no
    replies has none to answer, and its rule is the rule above.

The version of a criteria list:
    Nothing new: the ADDRESS every verdict row carries — ``criteria_layer`` +
    ``criteria_source_id`` (task 08, decision 21; ``judged_list.py`` reads a
    list back by it). A list's row is written once: an author's edit is a new
    override row, a recomposition releases the model's row and claims a new
    one. So one address is one version of the list, and the list in force now
    (:func:`~course_supporter.homework.criteria_list_service.load_in_force`,
    no model call) is compared by its address. An attempt with no verdict rows
    was judged against no list — a review by today's Mentor, or a review that
    was itself repeated — and is the source of no copy for a type judged
    against a list; the copy is taken from the attempt that was judged.

    A project on the new path is judged against a list as a text task is: its
    evaluation (``project_criteria_evaluation``) takes the list from the same
    service (``criteria_list_service``) and its verdict rows carry the same
    address. So its key carries that address too (project chain K8): an
    author's edit of a project's list reviews the same archive anew, as it
    does a text task's file.

What the submission gets:
    The earlier attempt's review — its structure, with
    ``unchanged_from_attempt`` set to that attempt's number and without the
    earlier review's section of replies (those answered the replies of the
    attempt before it; task 12a, K4), the markdown
    assembled from it by the one assembler (the earlier text, opened by the
    phrase ``review.unchanged``), and its score — and ``dedup_of``, the same
    number, in its attempt receipt, beside the ``evaluator_version`` of that
    review: the copy was written by the source's evaluator. The phrasebook's
    answers to the replies beside the work, if any, are kept with the copy,
    in the same transaction, as the explanation stage keeps its own. The body then
    finishes it as it finishes any reviewed revision: ``reviewing``,
    ``completed``, the webhook, ``delivered``; the portal reads the stored
    review as it reads any. The copy also carries what the doors of the source
    did not open — ``not_opened`` and ``recovered_encoding`` of its
    ``safety_result``: the work is the same, so the student sees the same files
    named as not read; nothing of the source's safety verdict is copied.

The course links of the copy (task 10, K6; the debt DD-SP-DE):
    The links to places of the course (``Remark.read``, a
    :class:`~course_supporter.models.review_structure.Reference` with a
    ``segment_id``) are in the copy as they were checked when the source was
    assembled. Between that and the copy the course may have changed — a
    fragment deleted, a material hidden or reprocessed — and a link to a place
    the student cannot open is worse than none. So, before the copy is stored,
    one read asks which of those places are candidates now
    (``DocumentSegmentRepository.still_candidates`` with the submission's
    ``course_node_id`` as the course root — the read
    :func:`~course_supporter.homework.course_link_delivery.settle_course_links`
    makes at its step 3), over the remarks of every section that has any
    (``fixed``, ``new_remarks``, ``open``, ``broken``), and a link whose place
    is no candidate is taken out of its remark's ``read``. A link with no
    ``segment_id`` (an outside address, ``url``) is not touched; the order of
    the rest is kept. There is no replacement: the copy gives the places of
    its source or none — it has no verdict rows of its own and no search row
    to look again by, and a new place would be another review. The copy stays
    free and out of the register (no model, no ``course_link_stale`` row);
    instead one log line, ``attempt_review_repeated_links_dropped``, names the
    number of links taken out and the ``item_id`` of the remarks that lost
    one — no titles, no texts. Nothing taken out, no line. A review with no
    course link costs no query. The text of a remark (``why``, ``todo``) may
    name a place in words; it is not mended, as on the ordinary path (the
    module ``course_link_delivery``). The source review is not changed — the
    copy is built from new remark objects, in the database and in memory.

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
    evaluator changed. A type on today's Mentor and a type with no dedup
    (``test``) keep the entry's check as it was.
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
from course_supporter.homework.remark_comments import CommentRecord, pending_replies
from course_supporter.homework.reply_source import (
    answer_without_the_model,
    beside_the_work,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.review_language import review_language_of
from course_supporter.models.review_schema import (
    REVIEW_SCHEMA_VERSION,
    review_schema_version,
)
from course_supporter.models.review_structure import (
    Remark,
    ReviewStructureV1,
)
from course_supporter.models.source import AssignmentType
from course_supporter.models.stored_read import read_stored
from course_supporter.storage.document_segment_repository import (
    DocumentSegmentRepository,
)
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

_REMARK_SECTIONS: Final = ("fixed", "new_remarks", "open", "broken")
"""The sections of a review that hold remarks, and so course links
(``Remark.read``)."""

_JUDGED_AGAINST_A_LIST: Final = frozenset(
    {
        AssignmentType.SHORT_TASK.value,
        AssignmentType.TASK.value,
        AssignmentType.PROJECT.value,
    }
)
"""The types whose review is judged against a criteria list: the text types
(task 09b) and a project, whose evaluation reads its list from the same service
(project chain K4)."""


def same_work_reaches_the_new_path(
    task_type: str, config: PathConfig | None = None
) -> bool:
    """Does the same work of this type go on to this dedup? (the module's rule)

    True for a type the new path serves (``served_by``) whose attempt adapter
    deduplicates (``dedup_enabled``) — ``task``, ``short_task``, and
    ``project`` while it is on the new path: the entry does not turn a
    byte-identical file away. False for every other
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
        door_reading: The source attempt's "not_opened" and "recovered_encoding".
        replies: The student's replies to answer, every one judged off the
            topic or hostile: the copy answers them with the phrasebook's
            words (task 12a, K4). Empty for a student with none.
    """

    submission_id: uuid.UUID
    attempt_no: int
    review: ReviewStructureV1
    score: int | None
    evaluator_version: str
    door_reading: dict[str, Any]
    replies: tuple[CommentRecord, ...] = ()


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
        update={"unchanged_from_attempt": source.attempt_no, "replies": []}
    )
    review, dropped = await _without_dead_course_links(
        session, review, submission_id=submission_id
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
    if source.door_reading:
        await hw_repo.store_safety_result(submission_id, dict(source.door_reading))
    if source.replies:
        submission = await hw_repo.get_by_id(submission_id)
        if submission is None:
            msg = f"HomeworkSubmission {submission_id} not found"
            raise ValueError(msg)
        await answer_without_the_model(
            session, submission, source.replies, language=review.language
        )
    if dropped.count:
        logger.info(
            "attempt_review_repeated_links_dropped",
            submission_id=str(submission_id),
            dedup_of=source.attempt_no,
            dropped=dropped.count,
            item_ids=list(dropped.item_ids),
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
    replies = await pending_replies(
        session,
        tenant_id=row.tenant_id,
        submission_id=submission_id,
        student_id=row.student_id,
        authored_document_id=row.authored_document_id,
        task_type=AssignmentType(row.task_type),
        config=config,
    )
    if replies and not beside_the_work(replies):
        # A reply about the work, or one the screen could not judge: a copy
        # would answer nothing the student wrote (task 12a, K3), and the work
        # is reviewed as usual. Every reply off the topic or hostile: the copy
        # all the same, with the phrasebook's answers (K4; the module).
        logger.info("attempt_dedup_replies_pending", submission_id=str(submission_id))
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

    submission = await session.get(HomeworkSubmission, submission_id)
    review_language = (
        await review_language_of(session, submission)
        if submission is not None
        else None
    )
    if review_language is None:
        # Not even the course root gives a language (a defect, the database
        # refuses such a root): nothing to compare a stored review's language
        # with, so no copy (the module; a missed repeat costs a review).
        logger.info("attempt_dedup_language_unknown", submission_id=str(submission_id))
        return None

    source = await HomeworkRepository(session).latest_unchanged_attempt(
        tenant_id=row.tenant_id,
        student_id=row.student_id,
        authored_document_id=row.authored_document_id,
        excluding=submission_id,
        fingerprint=row.fingerprint,
        task_version=row.task_version,
        criteria_list=criteria_list,
        evaluator_version=version,
        review_language=review_language,
    )
    if source is None or source.attempt_no is None:
        return None
    review = _version_1_review(source.review_result, submission_id=source.id)
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
        door_reading=_door_reading_of(source.safety_result),
        replies=tuple(replies),
    )


@dataclass(frozen=True, slots=True)
class _DroppedLinks:
    """What the check of a copy's course links took out: the log's two facts."""

    count: int = 0
    item_ids: tuple[str, ...] = ()


async def _without_dead_course_links(
    session: AsyncSession,
    review: ReviewStructureV1,
    *,
    submission_id: uuid.UUID,
) -> tuple[ReviewStructureV1, _DroppedLinks]:
    """``review`` without the course links whose place is no candidate now.

    The rule is the module's (the course links of the copy). One read of
    ``still_candidates`` for all the places of the review — none, and no query
    at all, when the review has no course link. ``review`` itself is not
    changed: a remark that loses a link is replaced by a copy of it, the rest
    stay as they are. A ``segment_id`` that is no identifier is no candidate.
    """
    places = {
        reference.segment_id
        for section in _REMARK_SECTIONS
        for remark in getattr(review, section)
        for reference in remark.read
        if reference.segment_id is not None
    }
    if not places:
        return review, _DroppedLinks()
    identifiers = {place: _as_uuid(place) for place in places}
    course_root_id = await session.scalar(
        select(HomeworkSubmission.course_node_id).where(
            HomeworkSubmission.id == submission_id
        )
    )
    live = (
        await DocumentSegmentRepository(session).still_candidates(
            course_root_id=course_root_id,
            segment_ids={found for found in identifiers.values() if found is not None},
        )
        if course_root_id is not None
        else set()
    )
    candidates = {place for place, found in identifiers.items() if found in live}

    count = 0
    item_ids: list[str] = []
    update: dict[str, list[Remark]] = {}
    for section in _REMARK_SECTIONS:
        remarks: list[Remark] = []
        for remark in getattr(review, section):
            kept = [
                reference
                for reference in remark.read
                if reference.segment_id is None or reference.segment_id in candidates
            ]
            if len(kept) != len(remark.read):
                count += len(remark.read) - len(kept)
                if remark.item_id is not None:
                    item_ids.append(remark.item_id)
                remark = remark.model_copy(update={"read": kept})
            remarks.append(remark)
        update[section] = remarks
    return review.model_copy(update=update), _DroppedLinks(count, tuple(item_ids))


def _as_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _door_reading_of(safety_result: object) -> dict[str, Any]:
    """Door readings of the source: not_opened and recovered_encoding only."""
    if not isinstance(safety_result, dict):
        return {}
    reading: dict[str, Any] = {}
    for key in ("not_opened", "recovered_encoding"):
        if key in safety_result:
            reading[key] = safety_result[key]
    return reading


def _evaluator_version_of(receipt: dict[str, Any] | None) -> str:
    """The evaluator's version a matched source's receipt names (the lookup's key)."""
    if receipt is None or not isinstance(
        version := receipt.get(EVALUATOR_VERSION_RECEIPT_KEY), str
    ):
        msg = "a source of a copy names no evaluator's version"
        raise ValueError(msg)
    return version


def _version_1_review(
    review_result: dict[str, Any] | None, *, submission_id: uuid.UUID | None = None
) -> ReviewStructureV1 | None:
    """The stored review as a version-1 structure, or ``None`` when it is not one.

    Read, not written: a field the models do not know — written by a newer
    release than this one — is left out, not a reason to review the same work
    anew and charge for it (:func:`~course_supporter.models.stored_read.read_stored`).
    ``submission_id`` is the source's, for that warning only.
    """
    if not review_result or review_schema_version(review_result) != (
        REVIEW_SCHEMA_VERSION
    ):
        return None
    try:
        return read_stored(
            ReviewStructureV1, review_result, submission_id=submission_id
        )
    except ValidationError:
        return None
