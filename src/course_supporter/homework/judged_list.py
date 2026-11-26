"""The criteria a submission's verdicts were judged against (task 09b, task 10).

Purpose:
    Two stages after the evaluation read the criteria back: the search for
    course links takes the text and the concepts of every failed criterion,
    and the explanation is shown every criterion with its verdict. Both read
    the very list the verdicts were judged against — by the address the
    verdict rows carry — never the list in force now, which an author's edit
    may have replaced since. One function does it for both, so the two cannot
    come to read two different lists.

    The pass rule of 2026-11-12 depends on whose list that was
    (``criteria_verdicts.pass_decision``); its layer is read from the same
    address, by the same function, so the rule and the list cannot come apart.
    So do the "must" criteria the author made mandatory themselves (the rule
    of 2026-11-17): read from the very list, never the one in force now.

Interface:
    :func:`read_judged_list` — the criteria, in the list's order; the
    author's each with ``must_by_author``.
    :func:`judged_layer` — whose list the rows were judged against.
    :func:`judged_author_musts` — the author's own "must" criteria of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    WeightCategory,
)
from course_supporter.homework.criteria_form import (
    author_criteria_from_document,
    author_musts,
    criteria_from_document,
)
from course_supporter.storage.orm import TaskCriteriaList, TaskCriteriaOverride

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.criteria_form import Criterion
    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )


def judged_layer(rows: Sequence[SubmissionCriterionVerdict]) -> CriteriaLayer:
    """Whose list the rows were judged against: the layer of their address.

    Raises:
        ValueError: The rows name more than one list, or none.
    """
    layer, _ = _judged_address(rows)
    return layer


def _judged_address(
    rows: Sequence[SubmissionCriterionVerdict],
) -> tuple[CriteriaLayer, uuid.UUID]:
    """The one list the rows name: its layer and its row's id."""
    addresses = {(row.criteria_layer, row.criteria_source_id) for row in rows}
    if len(addresses) != 1:
        msg = f"the verdict rows name {len(addresses)} lists, not one"
        raise ValueError(msg)
    ((layer, source_id),) = addresses
    return CriteriaLayer(layer), source_id


async def read_judged_list(
    session: AsyncSession,
    submission: HomeworkSubmission,
    rows: Sequence[SubmissionCriterionVerdict],
) -> tuple[Criterion, ...]:
    """The criteria the rows were judged against, read by the rows' address.

    A list's rows are written once — an author's new edit is a new row, and
    the model's list is stored whole when it is ready (task 08) — so the
    address gives the very list the evaluation used, soft-deleted or not.
    The list must belong to the submission's own task. The author's edit is
    read with ``must_by_author`` on each criterion, ``False`` for one stored
    before the mark (``criteria_form.author_criteria_from_document``).

    Raises:
        ValueError: The rows name more than one list, or a list that is not
            there, not this task's, or without criteria.
    """
    layer, source_id = _judged_address(rows)
    listed: TaskCriteriaOverride | TaskCriteriaList | None = (
        await session.get(TaskCriteriaOverride, source_id)
        if layer == CriteriaLayer.AUTHOR
        else await session.get(TaskCriteriaList, source_id)
    )
    if (
        listed is None
        or listed.authored_document_id != submission.authored_document_id
        or listed.criteria is None
    ):
        msg = f"the {layer} list {source_id} of the verdict rows is not this task's"
        raise ValueError(msg)
    if layer == CriteriaLayer.AUTHOR:
        return author_criteria_from_document(
            listed.criteria, submission_id=submission.id
        )
    return criteria_from_document(listed.criteria, submission_id=submission.id)


async def judged_author_musts(
    session: AsyncSession,
    submission: HomeworkSubmission,
    rows: Sequence[SubmissionCriterionVerdict],
) -> frozenset[str]:
    """The criteria the author made mandatory themselves, of the judged list.

    What the pass rule reads beside the layer
    (``criteria_verdicts.pass_decision``). Nothing is read when the answer is
    known from the rows: the model's list has no mark, and a list without a
    "must" row has no "must" to mark. Otherwise the list by the rows' address
    (:func:`read_judged_list`): one read of the edit's row by its primary key.

    Raises:
        ValueError: See :func:`read_judged_list`.
    """
    if judged_layer(rows) is not CriteriaLayer.AUTHOR or not any(
        row.item_kind == VerdictItemKind.CRITERION and row.weight == WeightCategory.MUST
        for row in rows
    ):
        return frozenset()
    return author_musts(await read_judged_list(session, submission, rows))
