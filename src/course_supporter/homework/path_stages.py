"""Who runs one stage of a submission path, and how one is added (task 03).

Purpose:
    The body of the new path walks a list of stage NAMES and knows nothing about
    what any of them means (architectural invariant 2). This module is the other
    half of that: a name resolves to an executor, the executor is handed one
    stage description and one submission context, and it answers what the stage
    decided.

Interface:
    An executor is ``async (StageContext) -> StageOutcome``. It receives the
    stage's own description from ``config/submission_paths.yaml`` — ladder,
    ceilings, prompt, limits — and returns either "carry on" or "the path ends
    here, in this state, for this reason". It writes its own verdict and nothing
    else: the submission's state and the run's checkpoint are the body's, and
    what happens next is the body's decision, never the executor's.

    :func:`register_stage_executor` adds one under a name,
    :func:`get_stage_executor` resolves one, and
    :func:`missing_stage_executors` is the startup check: a stage the
    configuration lists and nobody can run stops the boot instead of a
    submission (the same rule as a hole in the file, mentor-rebuild task 02).

Replacing one:
    Register a different function under the same name before the first
    submission is routed — the body resolves the name at each stage, so nothing
    else changes. The two executors here are thin on purpose: each asks today's
    own function to do the work, through the ONE additive argument that function
    grew (:class:`~course_supporter.llm.stage_router.StageExecution`), so the
    new path and today's Mentor cannot drift into two ideas of what a safety
    check or an attempt classifier is.

Extending:
    A new stage is a definition in the configuration plus a function registered
    here. Nothing in the body changes, and the startup check refuses the
    configuration until the function exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from course_supporter.llm.ladder_config import LadderEntry, StageConfig
from course_supporter.llm.stage_router import StageExecution

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.homework.doors import DoorReading
    from course_supporter.homework.path_config import PathKey, PathStage
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.security.schemas import CourseContext
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger(__name__)

SAFETY = "safety"
ATTEMPT_CLASSIFIER = "attempt_classifier"


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything an executor is given, and nothing it is not.

    No job id: an executor records its OWN verdict — the safety result, the
    classifier's verdict — because that trace belongs to the stage that produced
    it and to nothing else. What it must NOT touch is the submission's state or
    the run's checkpoint: those say where the whole path stands, and the body
    writes them in one place so they cannot be written from two.

    ``door`` is what the doors read (task 11, decision 9): ``submission_text``
    is its text, and the rest -- what was not opened, how the file was read,
    what the signal screen noticed -- is for the safety stage and the trace,
    never for a stage that reviews the work.
    """

    session: AsyncSession
    router: StageRouter
    submission: HomeworkSubmission
    submission_text: str
    language: str | None
    path_key: PathKey
    stage_name: str
    stage: PathStage
    door: DoorReading | None = None


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What a stage decided: carry on, or end the path here.

    ``terminal_status`` is a stored submission status (``mismatch``,
    ``rejected``…) and means the path is finished — not frozen, not failed:
    the stage reached an answer and that answer ends the submission.
    ``reason_code`` travels with it to the surface, which picks the words.
    """

    carry_on: bool
    terminal_status: str | None = None
    reason_code: str | None = None

    @classmethod
    def ok(cls) -> StageOutcome:
        """The stage is done and the next one may run."""
        return cls(carry_on=True)

    @classmethod
    def ends_path(cls, status: str, reason_code: str) -> StageOutcome:
        """The stage reached an answer that finishes the submission."""
        return cls(carry_on=False, terminal_status=status, reason_code=reason_code)


StageExecutor = Callable[[StageContext], Awaitable[StageOutcome]]

_EXECUTORS: dict[str, StageExecutor] = {}


def register_stage_executor(name: str, executor: StageExecutor) -> None:
    """Register (or replace) the executor for one stage name."""
    _EXECUTORS[name] = executor


def get_stage_executor(name: str) -> StageExecutor:
    """The executor for ``name``.

    Raises:
        KeyError: when no executor is registered — a configuration the startup
            check should have refused (:func:`missing_stage_executors`).
    """
    return _EXECUTORS[name]


def missing_stage_executors(stage_names: Collection[str]) -> list[str]:
    """Stage names the configuration lists that nobody can run, sorted.

    Empty means every described stage has an executor. The startup check turns a
    non-empty answer into a refusal to boot: a stage discovered at submission
    time would fail one student's work for a mistake made in a file.
    """
    return sorted(name for name in set(stage_names) if name not in _EXECUTORS)


def validate_stage_executors(stage_names: Collection[str]) -> None:
    """Refuse to boot when a described stage has nobody to run it.

    The startup twin of the configuration's own checks (mentor-rebuild task 02):
    a hole stops the boot, never a submission. Called beside
    :func:`~course_supporter.homework.path_config.validate_path_config` in both
    processes.

    Raises:
        ValueError: naming every stage without an executor at once.
    """
    missing = missing_stage_executors(stage_names)
    if missing:
        raise ValueError(
            "Submission path stages without an executor: "
            + ", ".join(f"'{name}'" for name in missing)
            + ". Register one in homework/path_stages.py, or remove the stage "
            "from config/submission_paths.yaml."
        )


def _execution(context: StageContext) -> StageExecution:
    """Map a path stage's description onto how the router should run it.

    ``PathStage`` and ``StageConfig`` carry the same router-facing fields by
    construction (mentor-rebuild task 03) and their rungs the same four, so this
    is a translation and not a decision. The two limits that have no counterpart
    on a ladder stage — no descent past a spent output ceiling, and the money
    ceiling — are what make it a path stage rather than a copy of one.
    """
    stage = context.stage
    return StageExecution(
        stage=StageConfig(
            prompt_ref=stage.prompt_ref,
            requires=list(stage.requires),
            ladder=[
                LadderEntry(
                    provider=rung.provider,
                    model=rung.model,
                    reasoning=rung.reasoning,
                    max_output_tokens=rung.max_output_tokens,
                )
                for rung in stage.ladder
            ],
            input_budget_ratio=stage.input_budget_ratio,
            record_output=stage.record_output,
        ),
        stage_name=context.stage_name,
        stop_on_output_ceiling=True,
        money_ceiling_usd=stage.ceilings.money_usd,
    )


async def run_safety_stage(context: StageContext) -> StageOutcome:
    """The model safety check, as a stage of the path.

    Today's own function does the work — prompt, parse, verdict — through its
    one additive argument, so the new path and today's Mentor cannot end up with
    two ideas of what is safe. It is shown what today's Mentor shows it (task
    11, decisions 9-10): the course and the topic the work is for, the signal
    screen's flags and the student's comment; and the verdict carries what the
    doors read onto the trace. What is this module's is the reaction: an unsafe
    verdict ends the submission at ``rejected``, and the reason code the surface
    phrases is the one the read path already knows for a Stage 2 refusal.
    """
    from course_supporter.homework.doors import carry_door_reading
    from course_supporter.security.stage2 import run_stage2_safety_check

    door = context.door
    verdict = await run_stage2_safety_check(
        context.submission_text,
        router=context.router,
        course_context=await _course_context(context.session, context.submission),
        execution=_execution(context),
        screen_flags=door.flags if door is not None else (),
        student_note=context.submission.student_note,
    )
    if door is not None:
        carry_door_reading(verdict, door)
    from course_supporter.storage.homework_repository import HomeworkRepository

    await HomeworkRepository(context.session).store_safety_result(
        context.submission.id, verdict.model_dump(mode="json")
    )
    if verdict.is_safe:
        return StageOutcome.ok()
    logger.info(
        "path_stage_safety_refused",
        submission_id=str(context.submission.id),
        violations=[v.value for v in verdict.violations],
    )
    return StageOutcome.ends_path("rejected", "stage2_rejected")


async def _course_context(
    session: AsyncSession, submission: HomeworkSubmission
) -> CourseContext:
    """The course and the topic the work is for, as today's Mentor builds it.

    The same four fields from the same two nodes (``api/tasks.py``); a node
    that is gone leaves its fields empty rather than failing the stage.
    """
    from course_supporter.security.schemas import CourseContext
    from course_supporter.storage.course_node_repository import (
        CourseNodeRepository,
    )

    nodes = CourseNodeRepository(session)
    course = await nodes.get_by_id(submission.course_node_id)
    target = await nodes.get_by_id(submission.node_id)
    return CourseContext(
        course_title=course.title if course else "",
        course_description=(course.description or "") if course else "",
        node_title=target.title if target else "",
        node_description=(target.description or "") if target else "",
    )


async def run_attempt_classifier_stage(context: StageContext) -> StageOutcome:
    """ "Does this look like an attempt at the task?", as a stage of the path.

    The same classifier and the same threshold as today
    (``config/sanity.yaml``): a high-confidence mismatch ends the submission,
    a match or a low-confidence mismatch carries on with nothing injected into
    what follows. The verdict is the student's answer, not an error — the
    portal has said so in those words since the doors pass, and the reason code
    is the one it already keys that article on.
    """
    from course_supporter.agents.sanity import SanityAgent
    from course_supporter.homework.sanity_config import get_sanity_config
    from course_supporter.homework.sanity_gate import is_gated
    from course_supporter.homework.task_context import load_task_context
    from course_supporter.language import display_name
    from course_supporter.storage.homework_repository import HomeworkRepository

    title, description, text = await load_task_context(
        context.session, context.submission.authored_document_id
    )
    classification = await SanityAgent(context.router).classify(
        task_title=title,
        task_description=description,
        task_text=text,
        submission_text=context.submission_text,
        language=display_name(context.language) if context.language else None,
        execution=_execution(context),
    )
    await HomeworkRepository(context.session).store_sanity_result(
        context.submission.id, classification.model_dump(mode="json")
    )
    if not is_gated(classification, get_sanity_config().confidence_threshold):
        return StageOutcome.ok()
    logger.info(
        "path_stage_attempt_classifier_gated",
        submission_id=str(context.submission.id),
        confidence=classification.confidence,
    )
    return StageOutcome.ends_path("mismatch", "mismatch")


register_stage_executor(SAFETY, run_safety_stage)
register_stage_executor(ATTEMPT_CLASSIFIER, run_attempt_classifier_stage)
