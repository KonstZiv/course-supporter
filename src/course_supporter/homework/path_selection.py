"""Which path a submission takes, and whether it takes one at all (task 03).

Purpose:
    The set and order of a submission's stages is chosen by code from what is
    known when it arrives — never by a model (architectural invariant 1). Two
    things decide: the assignment type, and where the submission stands. This
    module answers both, and the per-type switch that says whether the new path
    serves that type at all.

Interface:
    :func:`choose_path` is the single entry. It answers ``None`` when the type
    is still served by today's Mentor — the case in production after this task —
    and a :class:`PathChoice` otherwise: the key that was chosen and the stage
    names it resolves to, read once so the body does not look them up again.

    The key is written into the submission's checkpoint and the continuation
    uses the written one; nothing here is asked twice about the same revision
    (:mod:`course_supporter.homework.path_checkpoint`).

Extending:
    A new submission state is a member of
    :class:`~course_supporter.homework.path_config.SubmissionState`, a branch in
    :func:`resolve_submission_state`, and three new stage lists per described
    type in the configuration — the startup check refuses a type that describes
    some states and not others, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from course_supporter.homework.path_config import (
    PathKey,
    ServedBy,
    SubmissionState,
    get_path_config,
)
from course_supporter.homework.remark_comments import pending_replies
from course_supporter.storage.homework_repository import HomeworkRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_config import PathConfig
    from course_supporter.models.source import AssignmentType

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PathChoice:
    """The path a submission takes: its key and the stages that key resolves to.

    ``stages`` is a tuple, not the configuration's list, so what the body walks
    cannot be edited by whoever holds it.
    """

    key: PathKey
    stages: tuple[str, ...]


async def resolve_submission_state(
    session: AsyncSession,
    *,
    student_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    submission_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    task_type: AssignmentType | None = None,
    config: PathConfig | None = None,
) -> SubmissionState:
    """Where this submission stands, as far as choosing a path goes.

    ``FIRST`` until the student has had a review for this task
    (:meth:`~course_supporter.storage.homework_repository.HomeworkRepository.has_reviewed_revision`);
    after it, a repeat — ``REPEAT_WITH_REPLIES`` when the submission has
    replies of the student to answer
    (:func:`~course_supporter.homework.remark_comments.pending_replies`: the
    type takes replies, and the student's last review of the same version of
    the task has replies no review answered yet; task 12a, K3), and
    ``REPEAT_WITHOUT_REPLIES`` otherwise.

    The replies are asked about only for a submission that exists —
    ``submission_id``, its ``tenant_id`` and ``task_type`` given, as the body
    gives them; a
    caller that names no submission (the price quote, ``billing/quotes.py``)
    is answered without them. A type that takes no replies — every type in
    production until they are switched on with their answers (K4) and the
    portal (K5) — never reaches the third state, and its submissions ask
    nothing more of the database than before. A student's free-text note is
    not a reply.
    """
    repo = HomeworkRepository(session)
    repeat = await repo.has_reviewed_revision(
        student_id=student_id,
        authored_document_id=authored_document_id,
    )
    if not repeat:
        return SubmissionState.FIRST
    if (
        submission_id is not None
        and tenant_id is not None
        and task_type is not None
        and await pending_replies(
            session,
            tenant_id=tenant_id,
            submission_id=submission_id,
            student_id=student_id,
            authored_document_id=authored_document_id,
            task_type=task_type,
            config=config,
        )
    ):
        return SubmissionState.REPEAT_WITH_REPLIES
    return SubmissionState.REPEAT_WITHOUT_REPLIES


async def choose_path(
    session: AsyncSession,
    *,
    task_type: AssignmentType,
    student_id: uuid.UUID,
    authored_document_id: uuid.UUID,
    config: PathConfig | None = None,
    submission_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> PathChoice | None:
    """Choose the path for one submission, or ``None`` for today's Mentor.

    ``config`` defaults to the process-wide configuration the boot validated
    (:func:`~course_supporter.homework.path_config.get_path_config`); tests pass
    their own. ``submission_id`` and its ``tenant_id`` name the submission the
    path is for: only then can its state be the one with replies
    (:func:`resolve_submission_state`).

    The switch is read first: a type served by today's Mentor asks the database
    nothing, so a submission on that type costs exactly what it cost before this
    task. Only a type on the new path pays for the state query.

    A type on the new path always has all three of its states described — the
    startup check refuses a partial description — so the lookup below cannot
    miss, and a ``KeyError`` here would mean the boot check did not run.
    """
    cfg = config if config is not None else get_path_config()
    declared = cfg.task_types[task_type]
    if declared.served_by is not ServedBy.NEW_PATH:
        return None

    state = await resolve_submission_state(
        session,
        student_id=student_id,
        authored_document_id=authored_document_id,
        submission_id=submission_id,
        tenant_id=tenant_id,
        task_type=task_type,
        config=cfg,
    )
    key = PathKey(task_type, state)
    choice = PathChoice(key=key, stages=tuple(declared.paths[state]))
    logger.info(
        "submission_path_chosen",
        path_key=str(key),
        stages=list(choice.stages),
    )
    return choice
