"""What a submission has already done, so a continuation need not pay twice.

Purpose:
    A submission on the rebuilt Mentor's path can stop between stages — the
    money ran out, a rung answered nothing at its ceiling, a provider was away —
    and be picked up later. The checkpoint is what lets the second attempt start
    where the first stopped: which path was chosen, which stages are behind it,
    why it stopped, and how many times it has already been retried.

    The shape follows the one the Methodist run already uses
    (:mod:`course_supporter.storage.node_summary_run_state`) and so do its two
    decisions: ``Job.current_stage`` is NOT mirrored inside the JSON (it has its
    own column and would drift), and completion is signalled by the Job's own
    status, never by a sentinel stage value.

Interface:
    :class:`PathCheckpoint` is the JSON under ``Job.stage_progress``;
    :meth:`PathCheckpoint.to_jsonb` / :meth:`PathCheckpoint.from_jsonb` are the
    round trip, so the writer and the tests store identical bytes.

    :func:`save_checkpoint` writes it and COMMITS: a checkpoint that is not
    durable is not a checkpoint, and the body owns its own session (the seam
    closed its own before the body ran, so this is not the mid-pipeline write
    ``impl-rules#12`` forbids).

    :func:`load_checkpoint` reads it back from the LAST job of the revision,
    not from a job id handed in: a continuation runs in a new job, and the work
    it continues was recorded by the previous one.

    :meth:`PathCheckpoint.first_unfinished` is the reconciliation rule against a
    stage list that may have changed since — see its docstring.

Extending:
    A new freeze reason is a member of :class:`FreezeReason` and a reaction to
    it in the body; nothing else here changes. A new field is a field with a
    default, so a checkpoint written before it still reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from course_supporter.homework.path_config import PathKey, SubmissionState
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.llm.error_categories import LadderStop
from course_supporter.models.source import AssignmentType
from course_supporter.storage.job_repository import JobRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_declared_subject_type = JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING]
# Derive-or-verify, in the spirit of the execution seam's own guards: the
# mapping admits ``None`` (s3_cleanup has no natural subject), and a homework
# job that lost its subject type would make the lookup below silently match
# nothing — a continuation would then start from scratch and pay twice. Fail at
# import instead.
if _declared_subject_type is None:  # pragma: no cover — test-locked
    msg = (
        "JOB_SUBJECT_TYPE has no subject type for homework processing; the "
        "submission-path checkpoint is read by it."
    )
    raise RuntimeError(msg)

_SUBJECT_TYPE: Final[str] = _declared_subject_type


class StageState(StrEnum):
    """Where one stage of the path stands.

    Two values, not four: a stage is either behind the submission or it is not.
    Why it is not — frozen, failed, never reached — is one fact about the whole
    run (``frozen_stage`` + ``frozen_reason``), not a per-stage one, because a
    path stops at its first unfinished stage and never runs past it.
    """

    PENDING = "pending"
    DONE = "done"


class FreezeReason(StrEnum):
    """Why the path stopped between stages, as a code the surface can phrase.

    A code, not a sentence: the server keeps the reason, the portal keeps the
    words, and the two travel separately (language-rules).
    """

    AWAITING_FUNDS = "awaiting_funds"
    """The funds port refused before the first paid call."""

    STAGE_MONEY_CEILING = "stage_money_ceiling"
    """No rung of the stage could make one attempt within its money ceiling."""

    OUTPUT_CEILING = "output_ceiling"
    """A rung answered nothing with its output ceiling spent; no descent."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """Every rung failed for reasons that may pass — worth retrying."""


FREEZE_REASON_FOR_LADDER_STOP: Final[dict[LadderStop, FreezeReason]] = {
    LadderStop.EXHAUSTED: FreezeReason.PROVIDER_UNAVAILABLE,
    LadderStop.OUTPUT_CEILING: FreezeReason.OUTPUT_CEILING,
    LadderStop.MONEY_CEILING: FreezeReason.STAGE_MONEY_CEILING,
}
"""How a ladder's ending becomes the reason a revision is held.

Total by construction and guarded below: a new way for a ladder to end without
a decision about what it means for the student would otherwise be discovered by
a ``KeyError`` in the middle of someone's submission.

The three are not interchangeable: only ``EXHAUSTED`` is worth another attempt,
which is why the body reads this mapping rather than the exception's message.
"""

_unmapped = set(LadderStop) - set(FREEZE_REASON_FOR_LADDER_STOP)
if _unmapped:  # pragma: no cover — test-locked
    msg = (
        f"LadderStop members without a freeze reason: "
        f"{sorted(s.value for s in _unmapped)}. Decide what each new ending "
        f"means for the student before the path can meet it."
    )
    raise RuntimeError(msg)


class PathCheckpoint(BaseModel):
    """The JSON under ``Job.stage_progress`` for a submission on the new path.

    ``task_type`` + ``submission_state`` ARE the path key, stored as the two
    enums they are made of rather than as parsed text: the continuation needs a
    :class:`~course_supporter.homework.path_config.PathKey` back, and a parser
    is a place for the two forms to disagree. :attr:`path_key` rebuilds it, and
    ``str()`` of that is the very text the register's funds-port row carries.

    ``retries`` counts the body's own re-queues. It lives here and not in the
    queue because the queue's attempt budget is shared with every other job in
    the system, so a path that wants its own limit has to keep its own count.
    """

    model_config = ConfigDict(extra="forbid")

    task_type: AssignmentType
    submission_state: SubmissionState
    stages: dict[str, StageState] = Field(default_factory=dict)
    frozen_stage: str | None = None
    frozen_reason: FreezeReason | None = None
    retries: NonNegativeInt = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def path_key(self) -> PathKey:
        """The key this run was started on."""
        return PathKey(self.task_type, self.submission_state)

    @classmethod
    def started(cls, key: PathKey, stages: Sequence[str]) -> PathCheckpoint:
        """A checkpoint for a run about to begin: every stage pending."""
        return cls(
            task_type=key.task_type,
            submission_state=key.state,
            stages=dict.fromkeys(stages, StageState.PENDING),
        )

    def with_stage_done(self, stage_name: str) -> PathCheckpoint:
        """The same run with one more stage behind it, and nothing frozen."""
        return self.model_copy(
            update={
                "stages": {**self.stages, stage_name: StageState.DONE},
                "frozen_stage": None,
                "frozen_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )

    def frozen(self, stage_name: str, reason: FreezeReason) -> PathCheckpoint:
        """The same run, stopped at ``stage_name`` for ``reason``."""
        return self.model_copy(
            update={
                "frozen_stage": stage_name,
                "frozen_reason": reason,
                "updated_at": datetime.now(UTC),
            }
        )

    def retried(self) -> PathCheckpoint:
        """The same run with one more retry counted against its own limit."""
        return self.model_copy(
            update={"retries": self.retries + 1, "updated_at": datetime.now(UTC)}
        )

    def first_unfinished(self, stages: Sequence[str]) -> str | None:
        """The stage a continuation starts from, or ``None`` when none is left.

        Walks the stage list as it is TODAY, not as it was when the run started:
        a freeze through the money ceiling is lifted by editing the
        configuration, so the list is expected to have changed by the time
        anyone continues. Two rules make the two lists meet:

        * a stage the checkpoint does not know is **unfinished** — it was added
          after the freeze and has certainly not run;
        * a state for a stage no longer in the list is **ignored** — it was
          removed, and a run cannot be held up by work nobody asks for.

        Both rules err the same way: towards doing the work rather than towards
        skipping it on the strength of a record that does not match.
        """
        for name in stages:
            if self.stages.get(name) is not StageState.DONE:
                return name
        return None

    def to_jsonb(self) -> dict[str, Any]:
        """Serialize to a JSONB-compatible dict (enums / datetimes → str)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_jsonb(cls, payload: dict[str, Any]) -> PathCheckpoint:
        """Deserialize from ``Job.stage_progress`` content."""
        return cls.model_validate(payload)


async def save_checkpoint(
    session: AsyncSession,
    job_id: uuid.UUID,
    checkpoint: PathCheckpoint,
    *,
    current_stage: str | None,
) -> None:
    """Write the checkpoint durably, in the body's own session.

    Commits, because a checkpoint that a rollback can take away would let a
    continuation re-run a stage that was already paid for. This is the
    Methodist orchestrator's pattern (``update_stage`` + ``update_stage_progress``
    + ``commit`` after each unit of work), and it is safe here for the same
    reason it is safe there: nothing is holding a row lock on ``jobs`` across
    this call — the execution seam wrote ``active`` in its own session and
    closed it before the body started (``impl-rules#12``).

    ``current_stage`` goes to the Job's own column, never into the JSON beside
    it: one fact, one place.
    """
    repo = JobRepository(session)
    if current_stage is not None:
        await repo.update_stage(job_id, current_stage)
    await repo.update_stage_progress(job_id, checkpoint.to_jsonb())
    await session.commit()
    logger.info(
        "submission_path_checkpoint_saved",
        job_id=str(job_id),
        path_key=str(checkpoint.path_key),
        current_stage=current_stage,
        frozen_reason=(
            checkpoint.frozen_reason.value if checkpoint.frozen_reason else None
        ),
        retries=checkpoint.retries,
    )


async def load_checkpoint(
    session: AsyncSession, submission_id: uuid.UUID
) -> PathCheckpoint | None:
    """Read the checkpoint of a revision from its most recent job.

    ``None`` when the revision has no job, its latest job never wrote one (every
    submission on today's Mentor), or what is there is not a checkpoint of this
    shape — an unreadable record is treated as no record, so a run starts over
    rather than resuming from something nobody can read.
    """
    job = await JobRepository(session).get_latest_for_subject(
        _SUBJECT_TYPE, submission_id
    )
    if job is None or not isinstance(job.stage_progress, dict):
        return None
    try:
        return PathCheckpoint.from_jsonb(job.stage_progress)
    except ValueError:
        logger.warning(
            "submission_path_checkpoint_unreadable",
            job_id=str(job.id),
            submission_id=str(submission_id),
        )
        return None
