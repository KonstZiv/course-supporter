"""The criteria a submission's verdicts were judged against (task 09b, task 10).

Purpose:
    Two stages after the evaluation read the criteria back: the search for
    course links takes the text and the concepts of every failed criterion,
    and the explanation is shown every criterion with its verdict. Both read
    the very list the verdicts were judged against — by the address the
    verdict rows carry — never the list in force now, which an author's edit
    may have replaced since. One function does it for both, so the two cannot
    come to read two different lists.

Interface:
    :func:`read_judged_list` — the criteria, in the list's order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import criteria_from_document
from course_supporter.storage.orm import TaskCriteriaList, TaskCriteriaOverride

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.criteria_form import Criterion
    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )


async def read_judged_list(
    session: AsyncSession,
    submission: HomeworkSubmission,
    rows: Sequence[SubmissionCriterionVerdict],
) -> tuple[Criterion, ...]:
    """The criteria the rows were judged against, read by the rows' address.

    A list's rows are written once — an author's new edit is a new row, and
    the model's list is stored whole when it is ready (task 08) — so the
    address gives the very list the evaluation used, soft-deleted or not.
    The list must belong to the submission's own task.

    Raises:
        ValueError: The rows name more than one list, or a list that is not
            there, not this task's, or without criteria.
    """
    addresses = {(row.criteria_layer, row.criteria_source_id) for row in rows}
    if len(addresses) != 1:
        msg = f"the verdict rows name {len(addresses)} lists, not one"
        raise ValueError(msg)
    ((layer, source_id),) = addresses
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
    return criteria_from_document(listed.criteria)
