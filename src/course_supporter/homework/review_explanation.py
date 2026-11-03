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
       author's edit may have replaced since
       (:func:`~course_supporter.homework.judged_list.read_judged_list`).
    2. The facts
       (:func:`~course_supporter.homework.verdict_explanation.explanation_facts`).
    3. The course links the search step kept (task 10, K5): for each
       criterion that is not met, up to three places under local
       identifiers (:func:`links_to_show`). No row kept — a path that did not
       search — shows none.
    4. One request; the answer is checked against the facts on every rung, and
       one that disagrees is retried with the reason (``PRE-FLIGHT.md`` 9.7).
    5. An identifier the answer names that was not shown for that criterion
       is cut, and each cut leaves a row in the call register without a
       model call (``action = course_link_fabricated``; identifiers only). It
       is not a retry: a made-up link is not a fact the answer contradicts,
       and a retry would be paid for to win nothing the cut does not.
    6. The answer is kept as cut, in the language it was written in.

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
    ShownLink,
)
from course_supporter.homework.course_link_position import position_of
from course_supporter.homework.course_links import MAX_LINKS, FoundCourseLinks
from course_supporter.homework.judged_list import read_judged_list
from course_supporter.homework.path_stages import StageOutcome
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.verdict_explanation import (
    drop_unshown_links,
    explanation_facts,
)
from course_supporter.language import display_name, resolve_review_language
from course_supporter.service_logging import CourseLinkTrace, record_course_link_trace
from course_supporter.storage.orm import CourseNode
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from course_supporter.homework.path_stages import StageContext
    from course_supporter.llm.stage_router import StageExecution

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
    criteria = await read_judged_list(context.session, submission, rows)
    facts = explanation_facts(criteria, rows)
    kept_links = await verdicts.get_candidates(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    shown_links = links_to_show(
        facts.unmet,
        FoundCourseLinks.model_validate(kept_links.body)
        if kept_links is not None
        else FoundCourseLinks(criteria=()),
    )

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
            course_links=shown_links,
        ),
        execution=execution,
    )

    answer, fabricated = drop_unshown_links(
        explained.answer,
        {cid: {link.id for link in links} for cid, links in shown_links.items()},
    )
    for criterion_id, ids in fabricated.items():
        await record_course_link_trace(
            context.session_factory,
            kind=CourseLinkTrace.FABRICATED,
            criterion_id=criterion_id,
            ids=ids,
        )
    await verdicts.store_explanation(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        language=language,
        body=answer.model_dump(mode="json"),
    )
    log.info(
        "review_explanation_written",
        language=language,
        passed=facts.passed,
        remarks=len(answer.remarks),
        linked=sum(1 for remark in answer.remarks if remark.links),
        fabricated_links=sum(len(ids) for ids in fabricated.values()),
        mentor_voice=answer.mentor_voice is not None,
        provider=explained.provider,
        model=explained.model,
    )
    return StageOutcome.ok()


def links_to_show(
    unmet: Sequence[str], found: FoundCourseLinks
) -> dict[str, tuple[ShownLink, ...]]:
    """The places of the course the explanation is shown, by criterion id.

    For each criterion of ``unmet``, the first :data:`MAX_LINKS` places the
    search kept for it, best first, each under a local identifier: the
    criterion's id, ``.l`` and its rank from 1 (``c3.l1``). The same inputs
    give the same identifiers, so a later reader of the kept answer resolves
    them against the same kept places. A criterion without places maps to an
    empty tuple; places kept for a criterion that is met are not shown.
    """
    return {
        criterion_id: tuple(
            ShownLink(
                id=f"{criterion_id}.l{rank}",
                material=candidate.material_title,
                position=position_of(candidate.anchor, candidate.source_type),
                excerpt=candidate.excerpt,
            )
            for rank, candidate in enumerate(
                found.candidates_of(criterion_id)[:MAX_LINKS], start=1
            )
        )
        for criterion_id in unmet
    }


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
