"""Where an action belongs: its course and its task (BIL-E2, 5b).

Purpose:
    The one link "a movement of the journal → a course → a task", for the
    author's spending page (:mod:`.spending`) and the administrator's cost
    report by task (:mod:`.costs`). An action is what the journal and the
    estimates key it by (:mod:`course_supporter.cost_estimate.shadow`):

    * ``review`` — key: the submission → its task (``authored_document_id``)
      → the task's course (``course_root_id``);
    * ``task_preparation`` — key ``{task}:{version}:{kind}`` (or the task
      alone) → the task → its course;
    * ``processing`` — key: the job → the document it processes (subject
      ``authored_document``; ``project_base`` → the base's task) → its
      course; a job about a course node → the node's root. The document is
      a material, not a homework task: :attr:`Link.authored_document_id` is
      the processed document all the same, and the caller decides what to
      show.

    A key that leads nowhere — a row gone, a key of another shape — links to
    :data:`UNLINKED`: the caller's "without a course" basket, never lost.

    :func:`action_of_call` gives the action of a call in the register by its
    job, so dollars and units meet on the same link.

Read-only; one query per kind of action, whatever the number of keys.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from course_supporter.billing.kinds import ActionKind
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    HomeworkSubmission,
    Job,
    ProjectBase,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ActionRef:
    """One action: its kind and its key, as the journal keys it."""

    kind: ActionKind
    key: str


@dataclass(frozen=True, slots=True)
class Link:
    """The course (a root ``CourseNode``) and the document an action is about."""

    course_node_id: uuid.UUID | None
    authored_document_id: uuid.UUID | None


UNLINKED: Final = Link(None, None)
"""An action whose key leads to no course."""

PREPARATION_STAGES: Final[frozenset[str]] = frozenset(
    {"criteria_decomposition", "key_explanation"}
)
"""Calls of a task preparation, even when made inside a review's job."""

PROCESSING_JOB_TYPES: Final[tuple[str, ...]] = (
    JobType.DOCUMENT_PROCESSING.value,
    JobType.NODE_SUMMARY_REGENERATION.value,
    JobType.DOCUMENT_PREPARATION.value,
    JobType.BASE_NORMALIZE.value,
)
"""Jobs whose calls are a processing."""


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _task_of_preparation(key: str) -> uuid.UUID | None:
    """The task of a preparation key: what comes before its first ``:``."""
    return _uuid(key.split(":", 1)[0])


def action_of_call(
    *, job_id: uuid.UUID, job_type: str, subject_id: uuid.UUID | None
) -> ActionRef | None:
    """The action a call of the register belongs to, by its job.

    A call of a preparation stage inside a review's job is the preparation of
    the submission's task — linked through the submission, which leads to the
    same task. ``None`` — a job that is no action (a cleanup).
    """
    if job_type == JobType.HOMEWORK_PROCESSING.value and subject_id is not None:
        return ActionRef(ActionKind.REVIEW, str(subject_id))
    if job_type == JobType.KEY_EXPLANATION.value and subject_id is not None:
        return ActionRef(ActionKind.TASK_PREPARATION, str(subject_id))
    if job_type in PROCESSING_JOB_TYPES:
        return ActionRef(ActionKind.PROCESSING, str(job_id))
    return None


async def _documents(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Each document's course."""
    if not ids:
        return {}
    rows = await session.execute(
        select(AuthoredDocument.id, AuthoredDocument.course_root_id).where(
            AuthoredDocument.id.in_(ids)
        )
    )
    return dict(rows.tuples().all())


async def _roots(
    session: AsyncSession, node_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Each course node's root."""
    from course_supporter.storage.course_node_repository import CourseNodeRepository

    repo = CourseNodeRepository(session)
    found: dict[uuid.UUID, uuid.UUID] = {}
    for node_id in node_ids:
        root = await repo.get_root_for(node_id)
        if root is not None:
            found[node_id] = root.id
    return found


async def _link_reviews(
    session: AsyncSession, refs: list[ActionRef]
) -> dict[ActionRef, Link]:
    keys = {ref: _uuid(ref.key) for ref in refs}
    ids = {value for value in keys.values() if value is not None}
    if not ids:
        return {}
    rows = await session.execute(
        select(
            HomeworkSubmission.id,
            AuthoredDocument.id,
            AuthoredDocument.course_root_id,
        )
        .join(
            AuthoredDocument,
            AuthoredDocument.id == HomeworkSubmission.authored_document_id,
        )
        .where(HomeworkSubmission.id.in_(ids))
    )
    by_submission = {sub: Link(course, task) for sub, task, course in rows.tuples()}
    return {
        ref: by_submission[key]
        for ref, key in keys.items()
        if key is not None and key in by_submission
    }


async def _link_preparations(
    session: AsyncSession, refs: list[ActionRef]
) -> dict[ActionRef, Link]:
    tasks = {ref: _task_of_preparation(ref.key) for ref in refs}
    courses = await _documents(
        session, {task for task in tasks.values() if task is not None}
    )
    return {
        ref: Link(courses[task], task)
        for ref, task in tasks.items()
        if task is not None and task in courses
    }


async def _link_processing(
    session: AsyncSession, refs: list[ActionRef]
) -> dict[ActionRef, Link]:
    keys = {ref: _uuid(ref.key) for ref in refs}
    ids = {value for value in keys.values() if value is not None}
    if not ids:
        return {}
    jobs = {
        job_id: (subject_type, subject_id, node_id)
        for job_id, subject_type, subject_id, node_id in (
            await session.execute(
                select(
                    Job.id, Job.subject_type, Job.subject_id, Job.course_node_id
                ).where(Job.id.in_(ids))
            )
        ).tuples()
    }
    document_of: dict[uuid.UUID, uuid.UUID] = {}
    bases: dict[uuid.UUID, uuid.UUID] = {}
    nodes: dict[uuid.UUID, uuid.UUID] = {}
    for job_id, (subject_type, subject_id, node_id) in jobs.items():
        if subject_type == JOB_SUBJECT_TYPE[JobType.DOCUMENT_PROCESSING]:
            if subject_id is not None:
                document_of[job_id] = subject_id
        elif subject_type == JOB_SUBJECT_TYPE[JobType.BASE_NORMALIZE]:
            if subject_id is not None:
                bases[job_id] = subject_id
        elif node_id is not None:
            nodes[job_id] = node_id
    if bases:
        base_tasks = dict(
            (
                await session.execute(
                    select(ProjectBase.id, ProjectBase.authored_document_id).where(
                        ProjectBase.id.in_(set(bases.values()))
                    )
                )
            )
            .tuples()
            .all()
        )
        for job_id, base_id in bases.items():
            if base_id in base_tasks:
                document_of[job_id] = base_tasks[base_id]
    courses = await _documents(session, set(document_of.values()))
    roots = await _roots(session, set(nodes.values()))
    by_job: dict[uuid.UUID, Link] = {}
    for job_id, document in document_of.items():
        if document in courses:
            by_job[job_id] = Link(courses[document], document)
    for job_id, node_id in nodes.items():
        if node_id in roots:
            by_job[job_id] = Link(roots[node_id], None)
    return {
        ref: by_job[key]
        for ref, key in keys.items()
        if key is not None and key in by_job
    }


async def link_actions(
    session: AsyncSession, refs: Iterable[ActionRef]
) -> dict[ActionRef, Link]:
    """The course and the document of every action; :data:`UNLINKED` where none."""
    by_kind: dict[ActionKind, list[ActionRef]] = {kind: [] for kind in ActionKind}
    unique = set(refs)
    for ref in unique:
        by_kind[ref.kind].append(ref)
    found: dict[ActionRef, Link] = {}
    found |= await _link_reviews(session, by_kind[ActionKind.REVIEW])
    found |= await _link_preparations(session, by_kind[ActionKind.TASK_PREPARATION])
    found |= await _link_processing(session, by_kind[ActionKind.PROCESSING])
    return {ref: found.get(ref, UNLINKED) for ref in unique}
