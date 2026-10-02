"""The explanation stage of a text task: the verdicts, told to the student (09b).

Purpose:
    The second half of the new path's review of a ``task`` or a
    ``short_task`` (``PRE-PLAN.md`` decision 13): the verdicts the evaluation
    stage settled, with the score and the pass the code counts from them, go
    to a model that writes the review's reason, the remarks on the criteria
    that are not met, and the Mentor's own word — and may not contradict any
    of them (decision 5). The answer is kept for the builder of the review
    (``homework/text_result.py``).

Interface:
    :func:`explain_verdicts` — what the stage executor
    (``homework/path_stages.py``) hands its context and its execution to. It
    returns the stage's outcome and writes the explanation in the body's
    session, so it is committed together with the checkpoint that marks the
    stage done (``impl-rules#12``; ``homework/path_runner.py``).

Steps:
    1. The submission's verdict rows; the list they were judged against, by
       the address the rows carry — never the list in force now, which an
       author's edit may have replaced since.
    2. The facts
       (:func:`~course_supporter.homework.verdict_explanation.explanation_facts`).
    3. One request; the answer is checked against the facts on every rung, and
       one that disagrees is retried with the reason (``PRE-FLIGHT.md`` 9.7).
    4. The answer is kept, in the language it was written in.

Language (``PRE-FLIGHT.md`` 9.9):
    The student's — the path's own resolution: the language asked for on the
    submission, the student's preference, the task's language — and else the
    language of the course's root, which a task without one inherits.

Failure:
    A ladder that gives no answer that agrees with the facts raises
    ``LadderExhaustedError`` to the body, which holds or retries the stage;
    the evaluation stage is done by then and is not paid for twice. A
    submission without verdict rows, or rows the list does not match, is a
    defect of the path, not a verdict: ``ValueError``.

Replacing:
    The stage depends on the verdict repository, on the pure facts and check,
    and on the agent's one method
    (:class:`~course_supporter.agents.review_explainer.ReviewExplainerAgent`)
    — each replaceable without touching the others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from course_supporter.agents.review_explainer import (
    ExplanationInput,
    ReviewExplainerAgent,
)
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.homework.criteria_form import criteria_from_document
from course_supporter.homework.path_stages import StageOutcome
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.language import display_name, resolve_review_language
from course_supporter.storage.orm import (
    CourseNode,
    TaskCriteriaList,
    TaskCriteriaOverride,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.criteria_form import Criterion
    from course_supporter.homework.path_stages import StageContext
    from course_supporter.llm.stage_router import StageExecution
    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )

logger = structlog.get_logger(__name__)


async def explain_verdicts(
    context: StageContext, *, execution: StageExecution
) -> StageOutcome:
    """Explain the submission's verdicts to its student, and keep the answer.

    Args:
        context: The stage's context; ``submission_text`` is the work as the
            doors read it.
        execution: The stage's description as the router runs it.

    Returns:
        ``ok`` with the explanation written.

    Raises:
        LadderExhaustedError: No answer agreed with the facts (see the module).
        ValueError: The submission has no verdict rows, or rows the list they
            name does not match.
    """
    submission = context.submission
    log = logger.bind(submission_id=str(submission.id), stage=context.stage_name)

    verdicts = SubmissionVerdictRepository(context.session)
    rows = await verdicts.list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    if not rows:
        msg = (
            "the explanation stage needs the verdicts of the evaluation stage, "
            "and the submission has none"
        )
        raise ValueError(msg)
    criteria = await _judged_list(context.session, submission, rows)
    facts = explanation_facts(criteria, rows)

    language = await _explanation_language(context)
    title, description, text = await load_task_context(
        context.session, submission.authored_document_id
    )
    explained = await ReviewExplainerAgent(context.router).explain(
        ExplanationInput(
            task_title=title,
            task_description=description,
            task_text=text,
            facts=facts,
            submission_text=context.submission_text,
            language=display_name(language),
        ),
        execution=execution,
    )

    await verdicts.store_explanation(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        language=language,
        body=explained.answer.model_dump(mode="json"),
    )
    log.info(
        "review_explanation_written",
        language=language,
        passed=facts.passed,
        remarks=len(explained.answer.remarks),
        mentor_voice=explained.answer.mentor_voice is not None,
        provider=explained.provider,
        model=explained.model,
    )
    return StageOutcome.ok()


async def _judged_list(
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


async def _explanation_language(context: StageContext) -> str:
    """The language of the explanation as an ISO 639-3 code.

    The path's resolution first (``context.language``); without it, the
    course root's language, read only then.

    Raises:
        ValueError: Neither gives a language — a root without one is refused
            by the database and by every write of it, so this is a defect.
    """
    root_language: str | None = None
    if context.language is None:
        root = await context.session.get(CourseNode, context.submission.course_node_id)
        root_language = root.default_language if root is not None else None
    code = resolve_review_language(
        explicit=context.language, preferred=None, course=root_language
    ).code
    if code is None:
        msg = "neither the student nor the course root gives a language"
        raise ValueError(msg)
    return code
