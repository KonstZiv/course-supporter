"""The student's earlier attempt, as the review of the next one reads it (task 13).

Purpose:
    A review of a repeated attempt looks back at the attempt before it: the
    evaluation stage counts the state of each remark from its rows (task 12a),
    the review's builder words a fixed criterion by its explanation, the
    explanation is shown its own remark on a criterion still open (task 13,
    explanation side; the product owner's decision A of 2026-11-23), and the
    evaluation of a text type is shown what changed in the work and what the
    earlier review found (task 13, K4; the operator's decisions П1 and П2 of
    2026-11-23). The earlier attempt is read here, in one place, so every
    reader looks at the same attempt by the same rules.

Interface:
    :class:`EarlierReview` — the earlier attempt: its verdict rows and the
    remarks of its explanation, with that explanation's language.
    :func:`read_earlier_review` — reads it; ``None`` when there is none.
    :func:`previous_remarks` — the remarks the explanation of THIS attempt is
    shown, by criterion id: pure, no session.
    :class:`AttemptMemory` — what the evaluation of a repeated attempt is
    shown of the earlier one (K4): its verdicts, the difference of the work,
    and the notes on what is not shown and why.
    :func:`read_attempt_memory` — reads it; ``None`` when there is nothing to
    show. :func:`attempt_memory` — the same from what was read: pure.
    :class:`EarlierWorkSource` — where the earlier attempt's work is read
    from; :class:`DoorsEarlierWork` — production's: the attempt's file in S3,
    read by the doors' own code.

The rule of :func:`previous_remarks` — a criterion's earlier remark is shown
only when all of these hold, and never otherwise:

* the criterion's remark is ``open`` on its row now — credited neither at the
  earlier attempt nor now; another state, a point of a criterion, a criterion
  without a state: never;
* there is an earlier attempt (``SubmissionVerdictRepository.earlier_attempt``
  — the same read the evaluation stage counts the states against);
* it has a stored explanation, written in the language of this request: a
  remark in another language would be quoted to the student in it;
* that explanation has a remark with the criterion's id.

The memory of the evaluation (task 13, K4):
    Only the attempts of one student at one task, and only ``task`` and
    ``short_task`` — the evaluation stage asks for it on their two stages
    alone; a test has none, a project comes later. The earlier attempt is the
    LATEST earlier attempt that got a review with verdict rows, at any version
    of the task (``SubmissionVerdictRepository.earlier_attempt`` with
    ``same_version=False``): the version is one of the notes, not a key. The
    memory is a hint (П2): the model decides every item itself, nothing is
    carried over by code, and less is shown rather than something false:

    * no such attempt — no memory: the request is a first attempt's;
    * the criteria list changed since — another address (``criteria_layer`` +
      ``criteria_source_id``, as the rule of the copy compares it): its
      verdicts are not shown, since an item id may name another item now; the
      difference and the note ``criteria_changed`` stay;
    * the task changed since (``task_version``; an unknown version is never
      the same) — the note ``task_changed``; verdicts and difference stay;
    * the work was rewritten — (lines added + lines removed) / the larger of
      the two attempts' line counts above ``attempt_memory.rewritten_share``
      (``config/submission_paths.yaml``; 0.5): the difference is not shown,
      the note ``rewritten``; the verdicts stay;
    * the earlier work cannot be read — the difference is not shown, the note
      ``earlier_work_unavailable``; the verdicts stay. The log gets one line
      with the ids and the kind of the error — never its message, which may
      quote the work;
    * nothing left to show — neither verdicts nor difference (the list
      changed, and the work was rewritten or cannot be read): no memory.

    The verdicts are those of the items the model is asked about now
    (:func:`~course_supporter.homework.criteria_verdicts.judged_items`), as
    stored: a "met" with its quote, a "not met" with its sentence, and
    ``unconfirmed`` for a "met" no line bore out (``quote_not_found``). The
    text of the earlier review's remarks is never shown. The difference is
    :meth:`~course_supporter.homework.attempts.TextAttemptSnapshotter.diff` of
    the two attempts' snapshots — the line difference of their normalized
    texts, both read by the doors' own code.

Replacing:
    The readers depend on :func:`read_earlier_review`,
    :func:`read_attempt_memory` and their value types alone; another way of
    finding the earlier attempt replaces a function's body, not its
    signature. Another place the earlier work is kept in is another
    :class:`EarlierWorkSource`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

import structlog

from course_supporter.criteria_kinds import CriteriaLayer, VerdictItemKind, VerdictValue
from course_supporter.homework.attempts import (
    AttemptWork,
    TextAttemptSnapshotter,
    line_count,
)
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
    PreviousRemark,
)
from course_supporter.remark_kinds import RemarkState
from course_supporter.storage.orm import HomeworkSubmission
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.doors import DoorReading
    from course_supporter.storage.orm import SubmissionCriterionVerdict
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)


class StatedRow(Protocol):
    """What :func:`previous_remarks` reads of a verdict row of this attempt."""

    @property
    def item_id(self) -> str: ...

    @property
    def item_kind(self) -> str: ...

    @property
    def remark_state(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class EarlierReview:
    """The student's earlier attempt at the same version of the task.

    Attributes:
        submission_id: The earlier attempt's submission.
        rows: Its verdict rows, in the order they were written.
        language: The language of its explanation (ISO 639-3); ``None`` when
            it has no stored explanation.
        remarks: Its explanation's remarks by criterion id; empty without an
            explanation.
    """

    submission_id: uuid.UUID
    rows: tuple[SubmissionCriterionVerdict, ...]
    language: str | None
    remarks: Mapping[str, CriterionRemark]


async def read_earlier_review(
    session: AsyncSession, submission: HomeworkSubmission
) -> EarlierReview | None:
    """The attempt ``submission`` is compared with, and its explanation.

    The attempt is ``SubmissionVerdictRepository.earlier_attempt``'s — the
    latest earlier live attempt of the same student at the same version of
    the task that has verdict rows; its explanation, the row the explanation
    stage kept for it. Reads only.

    Returns:
        ``None`` when there is nothing to compare with.
    """
    stored = SubmissionVerdictRepository(session)
    attempt = await stored.earlier_attempt(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    if attempt is None:
        return None
    explained = await stored.get_explanation(
        tenant_id=submission.tenant_id, submission_id=attempt.submission_id
    )
    if explained is None:
        return EarlierReview(
            submission_id=attempt.submission_id,
            rows=attempt.rows,
            language=None,
            remarks=MappingProxyType({}),
        )
    remarks = ExplanationAnswer.model_validate(explained.body).remarks
    return EarlierReview(
        submission_id=attempt.submission_id,
        rows=attempt.rows,
        language=explained.language,
        remarks=MappingProxyType({remark.id: remark for remark in remarks}),
    )


def previous_remarks(
    rows: Iterable[StatedRow], earlier: EarlierReview | None, *, language: str
) -> dict[str, PreviousRemark]:
    """The earlier remarks the explanation is shown, by criterion id.

    See the module for the rule.

    Args:
        rows: This attempt's verdict rows, with the states the evaluation
            stage wrote on them.
        earlier: The earlier attempt (:func:`read_earlier_review`).
        language: The language of this request (ISO 639-3).
    """
    if earlier is None or earlier.language != language:
        return {}
    shown: dict[str, PreviousRemark] = {}
    for row in rows:
        if row.item_kind != VerdictItemKind.CRITERION:
            continue
        if row.remark_state != RemarkState.OPEN:
            continue
        remark = earlier.remarks.get(row.item_id)
        if remark is not None:
            shown[row.item_id] = PreviousRemark(
                what=remark.what, why=remark.why, todo=remark.todo
            )
    return shown


# ── The memory of the evaluation (task 13, K4) ──


class MemoryRow(Protocol):
    """What :func:`attempt_memory` reads of a verdict row of the earlier attempt."""

    @property
    def item_id(self) -> str: ...

    @property
    def verdict(self) -> str: ...

    @property
    def quote(self) -> str | None: ...

    @property
    def missing(self) -> str | None: ...

    @property
    def quote_not_found(self) -> bool: ...

    @property
    def criteria_layer(self) -> str: ...

    @property
    def criteria_source_id(self) -> uuid.UUID: ...


@dataclass(frozen=True, slots=True)
class PreviousVerdict:
    """The earlier review's verdict on one item, as the evaluation is shown it.

    Attributes:
        id: The item, as the list names it.
        verdict: As stored: ``met`` or ``not_met``.
        quote: A "met"'s quote of the earlier work; ``None`` for a "not met".
        missing: A "not met"'s sentence on what was missing (``None`` when it
            has none); ``None`` for a "met".
        unconfirmed: The model said "met" and no line of the work bore it out
            (the row's ``quote_not_found``): it counted as "not met".
    """

    id: str
    verdict: VerdictValue
    quote: str | None
    missing: str | None
    unconfirmed: bool


@dataclass(frozen=True, slots=True)
class AttemptMemory:
    """What the evaluation of a repeated attempt is shown of the earlier one.

    Attributes:
        earlier_submission_id: The earlier attempt.
        verdicts: Its verdicts on the items judged now, in their order;
            ``None`` when not shown — the criteria list changed.
        diff: The difference of the work, earlier against now, as a unified
            diff (``""`` — the same work); ``None`` when not shown — the work
            was rewritten, or the earlier work could not be read.
        criteria_changed: The list judged now is not the one the earlier
            attempt was judged against.
        task_changed: The task's version is not the earlier attempt's, or
            either is unknown.
        rewritten: More of the work changed than the share allowed.
        earlier_work_unavailable: The earlier work could not be read.
        changed_share: (lines added + lines removed) / the larger line count;
            ``None`` without the earlier work.
        lines_added: Lines only in this attempt (``0`` without the earlier work).
        lines_removed: Lines only in the earlier attempt (likewise).
    """

    earlier_submission_id: uuid.UUID
    verdicts: tuple[PreviousVerdict, ...] | None
    diff: str | None
    criteria_changed: bool
    task_changed: bool
    rewritten: bool
    earlier_work_unavailable: bool
    changed_share: float | None = None
    lines_added: int = 0
    lines_removed: int = 0


class EarlierWorkSource(Protocol):
    """Where the work of an earlier attempt is read from.

    ``read`` returns the work as the doors read it, or raises — whatever the
    cause, the memory then says the earlier work is unavailable.
    """

    async def read(
        self, session: AsyncSession, submission: HomeworkSubmission
    ) -> DoorReading: ...


class DoorsEarlierWork:
    """The earlier attempt's file from S3, read by the doors' own code.

    The parts of the doors that only read (``homework/path_runner.py``):
    :func:`~course_supporter.homework.path_runner.door_languages` for the
    attempt's own languages and
    :func:`~course_supporter.homework.path_runner.read_text_work` for the
    file — the code the body read it with when it was sent, as
    ``scripts/record_evaluation_input.py`` reads a submission. The download
    is a temporary file, deleted once read.
    """

    def __init__(self, s3: S3Client) -> None:
        self._s3 = s3

    async def read(
        self, session: AsyncSession, submission: HomeworkSubmission
    ) -> DoorReading:
        from course_supporter.homework.path_runner import (
            door_languages,
            read_text_work,
        )

        key = self._s3.extract_key(submission.file_url)
        if key is None:
            msg = "the earlier attempt's file_url names no S3 key"
            raise ValueError(msg)
        path = await self._s3.download_file(key)
        try:
            languages = await door_languages(session, submission)
            return read_text_work(
                filename=submission.original_filename or path.name,
                file_bytes=path.read_bytes(),
                languages=languages.verify,
                note_flags=(),
            )
        finally:
            path.unlink(missing_ok=True)


async def read_attempt_memory(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *,
    current: DoorReading,
    criteria_layer: CriteriaLayer,
    criteria_source_id: uuid.UUID,
    judged: Sequence[str],
    source: EarlierWorkSource | None,
    rewritten_share: float,
) -> AttemptMemory | None:
    """What the evaluation of ``submission`` is shown of the earlier attempt.

    See the module for the rules. Reads only; the earlier work is read from
    ``source`` (``None`` — no source at hand: the work is unavailable).

    Args:
        current: The work of this attempt, as the doors read it.
        criteria_layer: The list judged now — its layer…
        criteria_source_id: …and its row: the list's address.
        judged: The items the model is asked about now, in their order.
        rewritten_share: ``attempt_memory.rewritten_share`` of the path file.

    Returns:
        ``None`` when there is nothing to show.
    """
    stored = SubmissionVerdictRepository(session)
    attempt = await stored.earlier_attempt(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        same_version=False,
    )
    if attempt is None:
        return None
    earlier = await session.get(HomeworkSubmission, attempt.submission_id)
    if earlier is None:  # pragma: no cover - the row was just read by id
        return None
    earlier_work: DoorReading | None = None
    if source is not None:
        try:
            earlier_work = await source.read(session, earlier)
        except Exception as exc:
            # One line, and no text of the work: an error's message may quote
            # the bytes it failed on, so only its kind is written.
            logger.warning(
                "attempt_memory_earlier_work_unavailable",
                submission_id=str(submission.id),
                earlier_submission_id=str(earlier.id),
                error=type(exc).__name__,
                category=_category(exc),
            )
    else:
        logger.warning(
            "attempt_memory_earlier_work_unavailable",
            submission_id=str(submission.id),
            earlier_submission_id=str(earlier.id),
            error="no_source",
            category=None,
        )
    return attempt_memory(
        attempt.submission_id,
        attempt.rows,
        criteria_layer=criteria_layer,
        criteria_source_id=criteria_source_id,
        judged=judged,
        task_changed=_task_changed(earlier.task_version, submission.task_version),
        earlier_work=earlier_work,
        current_work=current,
        rewritten_share=rewritten_share,
    )


def attempt_memory(
    earlier_submission_id: uuid.UUID,
    rows: Sequence[MemoryRow],
    *,
    criteria_layer: CriteriaLayer,
    criteria_source_id: uuid.UUID,
    judged: Sequence[str],
    task_changed: bool,
    earlier_work: DoorReading | None,
    current_work: DoorReading,
    rewritten_share: float,
) -> AttemptMemory | None:
    """The memory from what was read: pure, no session.

    Args:
        rows: The earlier attempt's verdict rows.
        earlier_work: Its work as the doors read it; ``None`` — unavailable.
        current_work: This attempt's work as the doors read it.
        Others: as :func:`read_attempt_memory`.

    Returns:
        ``None`` when neither verdicts nor difference are left to show.
    """
    address = (criteria_layer.value, criteria_source_id)
    criteria_changed = not rows or any(
        (row.criteria_layer, row.criteria_source_id) != address for row in rows
    )
    verdicts = None if criteria_changed else _previous_verdicts(rows, judged)

    diff: str | None = None
    rewritten = False
    share: float | None = None
    added = removed = 0
    if earlier_work is not None:
        snapshotter = TextAttemptSnapshotter()
        before = snapshotter.snapshot(AttemptWork(reading=earlier_work))
        after = snapshotter.snapshot(AttemptWork(reading=current_work))
        change = snapshotter.diff(before, after)
        added, removed = change.lines_added, change.lines_removed
        share = changed_share(
            added,
            removed,
            lines_before=line_count(str(before.body)),
            lines_after=line_count(str(after.body)),
        )
        rewritten = share > rewritten_share
        diff = None if rewritten else change.unified_diff

    if verdicts is None and diff is None:
        return None
    return AttemptMemory(
        earlier_submission_id=earlier_submission_id,
        verdicts=verdicts,
        diff=diff,
        criteria_changed=criteria_changed,
        task_changed=task_changed,
        rewritten=rewritten,
        earlier_work_unavailable=earlier_work is None,
        changed_share=share,
        lines_added=added,
        lines_removed=removed,
    )


def changed_share(
    lines_added: int, lines_removed: int, *, lines_before: int, lines_after: int
) -> float:
    """(added + removed) / the larger of the two line counts; 0 for two empties.

    >>> changed_share(5, 5, lines_before=20, lines_after=20)
    0.5
    >>> changed_share(0, 0, lines_before=0, lines_after=0)
    0.0
    """
    larger = max(lines_before, lines_after)
    return (lines_added + lines_removed) / larger if larger else 0.0


def _previous_verdicts(
    rows: Sequence[MemoryRow], judged: Sequence[str]
) -> tuple[PreviousVerdict, ...]:
    """The rows of the items judged now, in their order, as stored.

    A criterion checked by points is judged through its points: its own row
    is the code's and is not one of them.
    """
    by_id = {row.item_id: row for row in rows}
    shown: list[PreviousVerdict] = []
    for item_id in judged:
        row = by_id.get(item_id)
        if row is None:
            continue
        verdict = VerdictValue(row.verdict)
        met = verdict is VerdictValue.MET
        shown.append(
            PreviousVerdict(
                id=item_id,
                verdict=verdict,
                quote=row.quote if met else None,
                missing=None if met else row.missing,
                unconfirmed=bool(row.quote_not_found),
            )
        )
    return tuple(shown)


def _task_changed(earlier: str | None, current: str | None) -> bool:
    """Another version of the task — or an unknown one, never the same."""
    return earlier is None or current is None or earlier != current


def _category(exc: Exception) -> str | None:
    """The doors' category of a refusal; ``None`` for any other error."""
    category = getattr(exc, "category", None)
    value = getattr(category, "value", None)
    return value if isinstance(value, str) else None
