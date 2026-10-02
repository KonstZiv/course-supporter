"""The evaluation stage of a text task: verdicts on the criteria in force (09b).

Purpose:
    The first half of the new path's review of a ``task`` or a ``short_task``:
    a verdict on every criterion of the list in force, each backed by a quote
    the code finds in the work or by a sentence on what is missing, settled by
    the rules of :mod:`course_supporter.homework.criteria_verdicts` and stored
    for the explanation stage and the review's builder.

Interface:
    :func:`evaluate_criteria` — what the stage executor
    (``homework/path_stages.py``) hands its context and its execution to. It
    returns the stage's outcome and writes the verdict rows in the body's
    session, so they are committed together with the checkpoint that marks the
    stage done (``impl-rules#12``; ``homework/path_runner.py``).

    :class:`CriteriaSource` — where the list in force comes from. Production
    composes it on a miss (:func:`build_criteria_list_service`); a test or
    another source hands its own.

Steps:
    1. The list in force, composed on a miss. None — the revision is held with
       :attr:`FreezeReason.CRITERIA_UNAVAILABLE` before this stage pays for
       anything (decision 6); the continuation that runs it again when a list
       appears is the next commit's (K4).
    2. One request about every item of the list.
    3. The verdicts that cannot stand as given — a quote not found, too long,
       too short, of several lines, missing; a "not met" without a sentence —
       are asked about once more, alone, each with its reason, of the rung
       that answered (``PRE-FLIGHT.md`` 9.5 (a); widened 2026-10-02).
    4. The verdicts are settled — the resubmission safeguard reads the latest
       earlier verdicts of the same list — and written.

Failure:
    No list is not a failure (step 1). A ladder that gives no answer — on
    either request — raises ``LadderExhaustedError`` to the body, which holds
    or retries the stage: a provider that fails must not turn into a "not met"
    for the student. An answer cut off at the output ceiling under the schema
    stops the ladder the same way (task 09a), and nothing is written.

Replacing:
    The stage depends on :class:`CriteriaSource`, on the agent's two methods
    (:class:`~course_supporter.agents.criteria_evaluator.CriteriaEvaluatorAgent`),
    on the pure rules and on the verdict repository — each replaceable without
    touching the others.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from course_supporter.agents.criteria_evaluator import (
    CriteriaEvaluatorAgent,
    Evaluated,
    EvaluationInput,
)
from course_supporter.criteria_kinds import VerdictValue
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    CriteriaUnavailable,
    build_criteria_list_service,
)
from course_supporter.homework.criteria_verdicts import (
    EvaluationAnswer,
    SubmittedWork,
    items_to_ask_again,
    settle_verdicts,
)
from course_supporter.homework.path_checkpoint import FreezeReason
from course_supporter.homework.path_stages import StageOutcome
from course_supporter.homework.task_context import load_task_context
from course_supporter.language import display_name
from course_supporter.storage.orm import AuthoredDocument, CourseNode
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.path_stages import StageContext
    from course_supporter.llm.stage_router import StageExecution
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)


class CriteriaSource(Protocol):
    """Where the evaluation stage takes the list in force from."""

    async def get_or_compose(
        self, authored_document_id: uuid.UUID
    ) -> CriteriaInForce | CriteriaUnavailable: ...


async def evaluate_criteria(
    context: StageContext,
    *,
    execution: StageExecution,
    criteria_source: CriteriaSource | None = None,
) -> StageOutcome:
    """Give the submission its verdicts on the criteria in force.

    Args:
        context: The stage's context; ``work`` must carry the files the doors
            read.
        execution: The stage's description as the router runs it.
        criteria_source: Where the list comes from; ``None`` — the production
            service, which composes the list on a miss in sessions of its own
            (``context.session_factory``).

    Returns:
        ``ok`` with the verdicts written; ``freezes`` when there is no list.

    Raises:
        LadderExhaustedError: A request got no answer (see the module).
        ValueError: ``context.work`` is empty — the doors read no files, so no
            quote could be found and every "met" would read "not met".
    """
    submission = context.submission
    log = logger.bind(submission_id=str(submission.id), stage=context.stage_name)

    source = (
        criteria_source
        if criteria_source is not None
        else build_criteria_list_service(context.session_factory, context.router)
    )
    listed = await source.get_or_compose(submission.authored_document_id)
    if not isinstance(listed, CriteriaInForce):
        log.info("criteria_evaluation_awaiting_criteria", reason=listed.reason.value)
        return StageOutcome.freezes(FreezeReason.CRITERIA_UNAVAILABLE)

    if not context.work:
        msg = (
            "the evaluation stage needs the work file by file, and the doors gave none"
        )
        raise ValueError(msg)
    work = SubmittedWork(context.work)

    title, description, text = await load_task_context(
        context.session, submission.authored_document_id
    )
    shown = EvaluationInput(
        task_title=title,
        task_description=description,
        task_text=text,
        criteria=listed.criteria,
        submission_text=context.submission_text,
        language=await _course_language(context.session, submission),
    )
    agent = CriteriaEvaluatorAgent(context.router)

    first = await agent.evaluate(shown, execution=execution)
    repeat: EvaluationAnswer | None = None
    to_repeat = items_to_ask_again(first.answer, work)
    if to_repeat:
        log.info(
            "criteria_evaluation_asking_again",
            items={item.id: item.reason.value for item in to_repeat},
            provider=first.provider,
            model=first.model,
        )
        again = await agent.ask_again(
            shown, to_repeat, first.answer, execution=_on_the_rung(execution, first)
        )
        repeat = again.answer

    verdicts = SubmissionVerdictRepository(context.session)
    earlier = await verdicts.previous_verdicts(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=listed.layer,
        criteria_source_id=listed.source_id,
    )
    records = settle_verdicts(listed, first.answer, repeat, work=work, earlier=earlier)
    await verdicts.replace_for_submission(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=listed.layer,
        criteria_source_id=listed.source_id,
        records=records,
    )
    _log_settled(log, records, first)
    return StageOutcome.ok()


def _on_the_rung(execution: StageExecution, evaluated: Evaluated) -> StageExecution:
    """The same stage, its ladder cut to the rung that gave the first answer.

    The model whose quotes did not stand is asked to mend them — not a
    stronger one, and not the whole ladder again (``PRE-FLIGHT.md`` 9.5 (a)).
    A ladder that lists one provider and model twice (with two reasoning
    forms, say) is cut to the first of them: the router reports which model
    answered, not which of its rungs.

    The repeat keeps the stage's money ceiling as its own: what is left of it
    after the first request lives inside the router's walk and does not cross
    to a second one. The ceiling was counted with the repeat inside it
    (``PRE-FLIGHT.md`` 9.6), so it bounds the repeat with room to spare.
    """
    for rung in execution.stage.ladder:
        if (rung.provider, rung.model) == (evaluated.provider, evaluated.model):
            return replace(
                execution,
                stage=execution.stage.model_copy(update={"ladder": [rung]}),
            )
    msg = (
        f"the rung that answered ({evaluated.provider}/{evaluated.model}) is not "
        "on the stage's ladder"
    )
    raise RuntimeError(msg)


async def _course_language(
    session: AsyncSession, submission: HomeworkSubmission
) -> str | None:
    """The course's language by name — for the sentences on what is missing.

    The language of the criteria list (``PRE-FLIGHT.md`` 9.9): the task's own,
    else the course root's, which a task inherits when it has none.
    """
    document = await session.get(AuthoredDocument, submission.authored_document_id)
    code = document.language if document is not None else None
    if not code:
        root = await session.get(CourseNode, submission.course_node_id)
        code = root.default_language if root is not None else None
    return display_name(code) if code else None


def _log_settled(log: Any, records: Sequence[VerdictRecord], first: Evaluated) -> None:
    """One event per flag a verdict row carries, then the summary.

    The safeguard's every firing is logged (decision 7): it is the one rule
    that can hide a real decline, and the log is where that shows. So is every
    quote that did not stand after the repeat — the flag for the author.
    """
    for record in records:
        if record.quote_not_found:
            log.info("criteria_evaluation_quote_not_found", item_id=record.item_id)
        if record.safeguard_submission_id is not None:
            log.info(
                "criteria_evaluation_safeguard_kept",
                item_id=record.item_id,
                model_verdict=(
                    record.model_verdict.value if record.model_verdict else None
                ),
                earlier_submission_id=str(record.safeguard_submission_id),
            )
    log.info(
        "criteria_evaluation_settled",
        rows=len(records),
        met=sum(1 for r in records if r.verdict is VerdictValue.MET),
        retried=sum(1 for r in records if r.retried),
        provider=first.provider,
        model=first.model,
    )
