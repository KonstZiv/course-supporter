"""Which attempt a submission is, written where the doors first read it (task 13, K2).

Purpose:
    The new Mentor is to know which attempt of the student it is reading and
    which one came before. This module records that once per submission, on
    the row itself, the moment the doors have read the work — on both roads
    (the new path's ``path_runner`` and today's Mentor in ``api.tasks``), which
    call :func:`record_attempt` with what their doors read. Nothing reads the
    record yet: the dedup (K3), the difference handed to the evaluation (K4)
    and the number in the API (K5) come later.

What is recorded:
    * ``attempt_no`` — the student's earlier attempts at the same task + 1. An
      attempt is a live submission that got a review (``completed`` /
      ``delivered`` — ``homework_repository.ATTEMPT_STATUSES``); a submission
      refused for safety, judged off-task or failed takes no number from the
      next one;
    * ``previous_submission_id`` — the latest such attempt, or ``None``;
    * ``task_version`` — the version of the task the work was read against
      (:func:`task_version_of`);
    * ``fingerprint`` — the K1 fingerprint of the work
      (:func:`~course_supporter.homework.attempts.attempt_snapshotter_for`);
    * ``attempt_receipt`` — the four above and ``task_version_changed``.

Once:
    A continuation, a retry or a run re-entered after a crash opens the doors
    again; the record of the first opening stands (the write is guarded by
    ``attempt_no IS NULL``). A submission the doors refused has no record: no
    work was read, and it is not an attempt either.

Known limitation — submissions in flight together:
    The number counts the attempts that HAD a review when the doors read the
    work. Two submissions of one student at one task whose doors both run
    before either is reviewed get the same number and the same previous
    attempt. A lock does not change that: neither ledger write changes what
    the other counts — the review that would is still running. Avoiding it
    needs the doors to hold a second submission until the first is reviewed,
    a change of the doors this commit does not make.

Failure:
    The record is bookkeeping no review reads yet, so it never fails one: an
    error is logged and the review goes on without a record (written inside a
    savepoint, so the caller's transaction is left as it was).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from course_supporter.homework.attempts import AttemptWork, attempt_snapshotter_for
from course_supporter.homework.test_doors import read_stored_answers
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer import manifest_from_jsonb
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument, HomeworkSubmission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.doors import DoorReading

__all__ = ["AttemptRecord", "record_attempt", "task_version_of"]

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """What the ledger wrote for one submission."""

    attempt_no: int
    previous_submission_id: uuid.UUID | None
    task_version: str | None
    task_version_changed: bool
    fingerprint: str

    def receipt(self) -> dict[str, Any]:
        """The receipt as stored in ``attempt_receipt`` (JSON)."""
        return {
            "attempt_no": self.attempt_no,
            "previous_submission_id": (
                str(self.previous_submission_id)
                if self.previous_submission_id is not None
                else None
            ),
            "task_version": self.task_version,
            "task_version_changed": self.task_version_changed,
            "fingerprint": self.fingerprint,
        }


def task_version_of(
    task_type: str, content_hash: str | None, reading: DoorReading
) -> str | None:
    """The version of the task the work was read against.

    Nothing new: the key the code already versions a task by. For a text or
    project task that is ``AuthoredDocument.content_hash`` — the version key
    of its criteria list and of the author's edit. A test's questions live in
    its published ``TestVersion``, which enters no ``content_hash``; the
    stored answers name the version they were accepted against, and that id
    is the test's version.
    """
    if task_type == AssignmentType.TEST.value:
        version_id, _ = read_stored_answers(reading.text)
        return str(version_id)
    return content_hash


async def record_attempt(
    session: AsyncSession,
    submission_id: uuid.UUID,
    reading: DoorReading,
) -> AttemptRecord | None:
    """Record which attempt the submission is; once, never failing the review.

    Call right after the doors read the work, before any stage. Returns what
    was written, or ``None`` when nothing was: the record already exists, or
    writing it failed (logged). The caller commits.
    """
    log = logger.bind(submission_id=str(submission_id))
    try:
        async with session.begin_nested():
            record = await _record(session, submission_id, reading)
    except Exception:
        log.exception("attempt_ledger_failed")
        return None
    if record is not None:
        log.info("attempt_recorded", **record.receipt())
    return record


async def _record(
    session: AsyncSession, submission_id: uuid.UUID, reading: DoorReading
) -> AttemptRecord | None:
    # Read by column, not from the caller's ORM object: the project's snapshot
    # was written by a Core UPDATE that object never saw, and refreshing it
    # here would change what the review reads from it.
    row = (
        await session.execute(
            select(
                HomeworkSubmission.attempt_no,
                HomeworkSubmission.student_id,
                HomeworkSubmission.authored_document_id,
                HomeworkSubmission.snapshot_hash,
                HomeworkSubmission.snapshot_manifest,
                AuthoredDocument.task_type,
                AuthoredDocument.content_hash,
            )
            .join(
                AuthoredDocument,
                AuthoredDocument.id == HomeworkSubmission.authored_document_id,
            )
            .where(HomeworkSubmission.id == submission_id)
        )
    ).one_or_none()
    if row is None or row.attempt_no is not None:
        return None

    work = AttemptWork(
        reading=reading,
        snapshot_hash=row.snapshot_hash,
        manifest=(
            manifest_from_jsonb(row.snapshot_manifest)
            if row.snapshot_manifest is not None
            else None
        ),
    )
    fingerprint = attempt_snapshotter_for(row.task_type).snapshot(work).fingerprint
    task_version = task_version_of(row.task_type, row.content_hash, reading)

    hw_repo = HomeworkRepository(session)
    earlier, previous_id, previous_version = await hw_repo.attempts_before(
        student_id=row.student_id,
        authored_document_id=row.authored_document_id,
        excluding=submission_id,
    )
    record = AttemptRecord(
        attempt_no=earlier + 1,
        previous_submission_id=previous_id,
        task_version=task_version,
        # An attempt recorded before K2 has no version to compare with: an
        # unknown version is not a changed one.
        task_version_changed=(
            previous_version is not None
            and task_version is not None
            and previous_version != task_version
        ),
        fingerprint=fingerprint,
    )
    written = await hw_repo.store_attempt(
        submission_id,
        attempt_no=record.attempt_no,
        previous_submission_id=record.previous_submission_id,
        task_version=record.task_version,
        fingerprint=record.fingerprint,
        attempt_receipt=record.receipt(),
    )
    return record if written else None
