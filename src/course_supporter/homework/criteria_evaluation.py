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
       anything (decision 6). One — the other revisions of the task held for
       it are put back in the queue (``PRE-FLIGHT.md`` 9.2, entry 1).
    2. One request about every item of the list.
    3. The verdicts that cannot stand as given — a quote not found, too long,
       too short, of several lines, missing, or the author's (a project's
       base, chain K3); a "not met" without a sentence — are asked about once
       more, alone, each with its reason, of the rung that answered
       (``PRE-FLIGHT.md`` 9.5 (a); widened 2026-10-02).
    4. The verdicts are settled — the resubmission safeguard reads the latest
       earlier verdicts of the same list — and written, each with its
       remark's state since the student's earlier attempt (task 12a). On a
       project's stage a "not met" keeps the place of its problem in the
       student's files when its line is there (chain K5); nothing is asked
       again for one that is not. A project's request reads its whole
       changed files with the author's lines marked, when the marked text
       fits the project's input budget, and without a mark when it does not
       (chain K5).

The earlier attempt (task 13, K4):
    On the two stages of the text types alone — ``criteria_evaluation`` and
    ``criteria_evaluation_with_replies``, by the stage's name as the switches
    above — and on a repeated attempt alone (a path of a ``repeat_*`` state),
    the first request is also shown what the earlier attempt's review found
    and what changed in the work since
    (:func:`~course_supporter.homework.earlier_review.read_attempt_memory`):
    a hint where to look, never an answer — nothing is carried over by code,
    the verdicts are settled as ever, the safeguard included. The second
    request about some items is what it was. No earlier attempt, or nothing
    of it left to show — the request is a first attempt's. A project's stage
    and a test are shown nothing.

The student's replies (task 12a, K4):
    On ``criteria_evaluation_with_replies`` alone — the switch by the stage's
    name, as the project's — the request also shows the student's replies
    that await an answer, on the topic and not hostile
    (:func:`~course_supporter.homework.student_replies.replies_for_evaluation`),
    read from :class:`~course_supporter.homework.reply_source.ReplySource`.
    Nothing after the answer changes: a "met" still stands on a quote of the
    student's files, so a reply can move a verdict only by pointing to a line
    the work has.

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
    request_chars,
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
    judged_items,
    settle_verdicts,
    with_remark_states,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.earlier_review import fit_memory, read_attempt_memory
from course_supporter.homework.mentor_context import mark_authors_lines
from course_supporter.homework.path_checkpoint import FreezeReason
from course_supporter.homework.path_config import (
    ModelStage,
    SubmissionState,
    get_path_config,
)
from course_supporter.homework.path_continuation import resume_awaiting_criteria
from course_supporter.homework.path_stages import (
    CRITERIA_EVALUATION,
    CRITERIA_EVALUATION_WITH_REPLIES,
    PROJECT_CRITERIA_EVALUATION,
    StageOutcome,
)
from course_supporter.homework.reply_source import default_reply_source
from course_supporter.homework.student_replies import replies_for_evaluation
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.text_budget import project_path_budget_chars
from course_supporter.language import display_name
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument, CourseNode
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.earlier_review import AttemptMemory
    from course_supporter.homework.path_stages import StageContext
    from course_supporter.homework.reply_source import ReplySource
    from course_supporter.homework.student_replies import EvaluationReply
    from course_supporter.llm.stage_router import StageExecution
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)

MEMORY_STAGES: frozenset[str] = frozenset(
    {CRITERIA_EVALUATION, CRITERIA_EVALUATION_WITH_REPLIES}
)
"""The stages shown the earlier attempt (task 13, K4): the text types' two."""


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
    reply_source: ReplySource | None = None,
) -> StageOutcome:
    """Give the submission its verdicts on the criteria in force.

    Args:
        context: The stage's context; ``work`` must carry the files the doors
            read. For a project, ``door.base_files`` is the author's base its
            quotes are told apart from (chain K3).
        execution: The stage's description as the router runs it.
        criteria_source: Where the list comes from; ``None`` — the production
            service, which composes the list on a miss in sessions of its own
            (``context.session_factory``).
        reply_source: Where the student's replies come from, on the stage
            with replies alone; ``None`` — the production source
            (:func:`~course_supporter.homework.reply_source.default_reply_source`).

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
        else build_criteria_list_service(
            context.session_factory, context.router, shadow_estimate=True
        )
    )
    listed = await source.get_or_compose(submission.authored_document_id)
    if not isinstance(listed, CriteriaInForce):
        log.info("criteria_evaluation_awaiting_criteria", reason=listed.reason.value)
        return StageOutcome.freezes(FreezeReason.CRITERIA_UNAVAILABLE)
    await _continue_the_waiting(context, log=log)

    if not context.work:
        msg = (
            "the evaluation stage needs the work file by file, and the doors gave none"
        )
        raise ValueError(msg)
    base = context.door.base_files if context.door is not None else ()
    work = SubmittedWork(context.work, base=base)
    # The one switch of project chain K5, by the stage's name. The project's
    # prompt reads the marks of the author's lines in a whole changed file
    # and asks a "not met" for the line where its problem stands, which its
    # review shows. The text types' prompt is calibrated: their input, rows
    # and reviews stay byte for byte what they were.
    project = context.stage_name == PROJECT_CRITERIA_EVALUATION

    title, description, text = await load_task_context(
        context.session, submission.authored_document_id
    )
    shown = EvaluationInput(
        task_title=title,
        task_description=description,
        task_text=text,
        criteria=listed.criteria,
        submission_text=_marked(context) if project else context.submission_text,
        language=await course_language(context.session, submission),
        replies=await _replies_shown(context, reply_source, log=log),
    )
    shown = replace(shown, memory=await _memory_shown(context, listed, shown, log=log))
    agent = CriteriaEvaluatorAgent(context.router)

    first = await agent.evaluate(shown, execution=execution)
    again: Evaluated | None = None
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
    records = settle_verdicts(
        listed,
        first.answer,
        repeat,
        work=work,
        earlier=earlier,
        place_unmet=project,
        first_call_id=first.call_id,
        repeat_call_id=again.call_id if again is not None else None,
    )
    # The state of each remark since the earlier attempt (task 12a), written
    # with the rows: counted anew whenever the stage writes them.
    attempt = await verdicts.earlier_attempt(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    records = with_remark_states(records, attempt.rows if attempt is not None else None)
    await verdicts.replace_for_submission(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=listed.layer,
        criteria_source_id=listed.source_id,
        records=records,
    )
    _log_settled(log, records, first)
    return StageOutcome.ok()


async def _replies_shown(
    context: StageContext, source: ReplySource | None, *, log: Any
) -> tuple[EvaluationReply, ...] | None:
    """The student's replies the request shows: on the stage with replies alone.

    ``None`` on every other stage, whose request has no slot for them and
    stays byte for byte what it was (task 12a, K4).
    """
    if context.stage_name != CRITERIA_EVALUATION_WITH_REPLIES:
        return None
    reader = source if source is not None else default_reply_source()
    read = await reader.replies(
        context.session,
        context.submission,
        task_type=context.path_key.task_type,
        config=context.config,
    )
    shown = replies_for_evaluation(read.entries)
    log.info(
        "criteria_evaluation_replies_shown",
        replies=len(shown),
        unjudged=len(read.unjudged),
    )
    return shown


async def _memory_shown(
    context: StageContext,
    listed: CriteriaInForce,
    shown: EvaluationInput,
    *,
    log: Any,
) -> AttemptMemory | None:
    """What the first request is shown of the earlier attempt (task 13, K4).

    ``None`` on every stage but the text types' two and on a first attempt:
    their request stays byte for byte what it was. The current work is the
    doors' reading (``context.door``); a context without one — a test — is
    read from its text and files. The share above which the work counts as
    rewritten, the lines of context and the ceiling of the growth are the
    path file's (``attempt_memory``). The ceiling is measured on this stage's
    own request — ``shown`` (everything but the memory) rendered by its
    prompt with the memory and without it
    (:func:`~course_supporter.homework.earlier_review.fit_memory`); why the
    difference or the memory is not shown is logged with the numbers and no
    text of the work.

    Чи показано пам'ять і чому — пишеться також у квитанцію спроби
    (``attempt_receipt["memory"]``, DD-SP-DP).
    """  # noqa: RUF002
    if context.stage_name not in MEMORY_STAGES:
        return None
    if context.path_key.state is SubmissionState.FIRST:
        return None
    config = context.config if context.config is not None else get_path_config()
    current = (
        context.door
        if context.door is not None
        else DoorReading(text=context.submission_text, files=context.work)
    )
    settings = config.attempt_memory
    judged = judged_items(listed.criteria)
    read = await read_attempt_memory(
        context.session,
        context.submission,
        current=current,
        criteria_layer=listed.layer,
        criteria_source_id=listed.source_id,
        judged=judged,
        source=context.earlier_work,
        rewritten_share=settings.rewritten_share,
        context_lines=settings.diff_context_lines,
    )
    if read is None:
        log.info("criteria_evaluation_memory_none", reason=None)
        await _store_memory_mark(context, reason="nothing_to_show")
        return None
    stage = context.stage
    if not isinstance(stage, ModelStage):  # pragma: no cover - a model stage
        msg = f"{context.stage_name} is not a model stage"
        raise TypeError(msg)
    fitted = fit_memory(
        read,
        request_chars=lambda memory: request_chars(
            replace(shown, memory=memory), prompt_ref=stage.prompt_ref, expected=judged
        ),
        max_growth=settings.max_growth,
    )
    memory = fitted.memory
    if memory is None:
        log.info(
            "criteria_evaluation_memory_none",
            reason=fitted.reason,
            base_chars=fitted.base_chars,
            growth=round(fitted.growth, 4),
            max_growth=settings.max_growth,
        )
        await _store_memory_mark(context, reason=fitted.reason)
        return None
    log.info(
        "criteria_evaluation_memory_shown",
        reason=fitted.reason,
        base_chars=fitted.base_chars,
        growth=round(fitted.growth, 4),
        sent_growth=round(fitted.sent_growth, 4),
        max_growth=settings.max_growth,
        earlier_submission_id=str(memory.earlier_submission_id),
        verdicts=None if memory.verdicts is None else len(memory.verdicts),
        diff_chars=None if memory.diff is None else len(memory.diff),
        lines_added=memory.lines_added,
        lines_removed=memory.lines_removed,
        changed_share=memory.changed_share,
        criteria_changed=memory.criteria_changed,
        task_changed=memory.task_changed,
        rewritten=memory.rewritten,
        earlier_work_unavailable=memory.earlier_work_unavailable,
    )
    await _store_memory_mark(context, reason=fitted.reason, memory=memory)
    return memory


async def _store_memory_mark(
    context: StageContext,
    *,
    reason: str | None,
    memory: AttemptMemory | None = None,
) -> None:
    """Write the codes-and-numbers memory mark into ``attempt_receipt``."""
    mark: dict[str, Any] = {
        "shown": memory is not None,
        "reason": reason,
        "earlier_submission_id": (
            None if memory is None else str(memory.earlier_submission_id)
        ),
        "verdicts": (
            None if memory is None or memory.verdicts is None else len(memory.verdicts)
        ),
        "diff_chars": (
            None if memory is None or memory.diff is None else len(memory.diff)
        ),
    }
    await HomeworkRepository(context.session).store_attempt_memory(
        context.submission.id, memory=mark
    )


def _marked(context: StageContext) -> str:
    """A project's delta context with the author's lines marked, when it fits.

    The marks of chain K5
    (:func:`~course_supporter.homework.mentor_context.mark_authors_lines`),
    over the student's and the base's files the door read; all or nothing
    against the project's new-path input budget
    (:func:`~course_supporter.homework.text_budget.project_path_budget_chars`):
    a context that would not fit marked goes as the door read it.
    """
    door = context.door
    if door is None:
        return context.submission_text
    return mark_authors_lines(
        context.submission_text,
        student={file.name: file.text for file in door.files},
        base={file.name: file.text for file in door.base_files},
        budget_chars=project_path_budget_chars(),
    )


async def _continue_the_waiting(context: StageContext, *, log: Any) -> None:
    """Put back the task's revisions that wait for the list this stage just got.

    Entry 1 of three (``PRE-FLIGHT.md`` 9.2): the composition that ends their
    wait is usually this very stage's. Right after the list is got, not after
    the verdicts: the list is in force whatever this evaluation's fate.

    In a session of its own: the body's carries this stage's verdict rows to
    the commit that marks the stage done. Best-effort: a queue that refuses
    must not cost this submission its review — the author's edit and the
    worker's start are the other two entries.
    """
    if context.arq is None:
        return
    submission = context.submission
    try:
        async with context.session_factory() as session:
            await resume_awaiting_criteria(
                session,
                context.arq,
                tenant_id=submission.tenant_id,
                authored_document_id=submission.authored_document_id,
            )
    except Exception:
        log.warning("criteria_evaluation_continuation_failed", exc_info=True)


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

    The execution is marked as an item repeat on that rung's position
    (``repeat_on_rung``): the register then tells the repeat from the first
    request and keeps the rung's place on the whole ladder. What is sent is
    the same.
    """
    for position, rung in enumerate(execution.stage.ladder, start=1):
        if (rung.provider, rung.model) == (evaluated.provider, evaluated.model):
            return replace(
                execution,
                stage=execution.stage.model_copy(update={"ladder": [rung]}),
                repeat_on_rung=position,
            )
    msg = (
        f"the rung that answered ({evaluated.provider}/{evaluated.model}) is not "
        "on the stage's ladder"
    )
    raise RuntimeError(msg)


async def course_language(
    session: AsyncSession, submission: HomeworkSubmission
) -> str | None:
    """The course's language by name — for the sentences on what is missing.

    The language of the criteria list (``PRE-FLIGHT.md`` 9.9): the task's own,
    else the course root's, which a task inherits when it has none. Public so
    ``scripts/record_evaluation_input.py`` reads it as the stage does.
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
    quote that did not stand after the repeat, and every item that fell for a
    quote of the project's base — the flags for the author.
    """
    for record in records:
        if record.quote_not_found:
            log.info("criteria_evaluation_quote_not_found", item_id=record.item_id)
        if record.quote_from_base:
            log.info("criteria_evaluation_quote_from_base", item_id=record.item_id)
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
