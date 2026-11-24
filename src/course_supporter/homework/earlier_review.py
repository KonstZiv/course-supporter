"""The student's earlier attempt, as the review of the next one reads it (task 13).

Purpose:
    A review of a repeated attempt looks back at the attempt before it: the
    evaluation stage counts the state of each remark from its rows (task 12a),
    the review's builder words a fixed criterion by its explanation, and the
    explanation is shown its own remark on a criterion still open (task 13,
    explanation side; the product owner's decision A of 2026-11-23). The
    earlier attempt is read here, in one place, so every reader looks at the
    same attempt.

Interface:
    :class:`EarlierReview` — the earlier attempt: its verdict rows and the
    remarks of its explanation, with that explanation's language.
    :func:`read_earlier_review` — reads it; ``None`` when there is none.
    :func:`previous_remarks` — the remarks the explanation of THIS attempt is
    shown, by criterion id: pure, no session.

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

Replacing:
    The readers depend on :func:`read_earlier_review` and on
    :class:`EarlierReview` alone; another way of finding the earlier attempt
    replaces the function's body, not its signature.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from course_supporter.criteria_kinds import VerdictItemKind
from course_supporter.homework.verdict_explanation import (
    CriterionRemark,
    ExplanationAnswer,
    PreviousRemark,
)
from course_supporter.models.stored_read import read_stored
from course_supporter.remark_kinds import RemarkState
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )


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
    stage kept for it, read as every stored row is
    (:func:`~course_supporter.models.stored_read.read_stored`): a field the
    model does not know is left out and logged by its path, never a refusal
    of the row. Reads only.

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
    # Read leniently, as every reader of a stored explanation (``read_stored``):
    # a field a newer release added is left out, never a refusal of the row.
    remarks = read_stored(
        ExplanationAnswer, explained.body, submission_id=attempt.submission_id
    ).remarks
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
