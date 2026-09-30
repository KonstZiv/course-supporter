"""Criteria decomposition agent (sprint-mentor T4, vision §1386 / D11; task 08).

Decomposes a task ``AuthoredDocument`` into the discrete, checkable
criteria a review scores a submission against, in the form of
:mod:`course_supporter.homework.criteria_form`. This agent is a pure LLM
transform: it takes the already-loaded task in its course and returns the
composed list. Composing it once per task version — the claim, the
heartbeat, the storage — is the job of
:mod:`course_supporter.homework.criteria_list_service`; keeping this class
DB-free makes the compute path unit-testable by stubbing the
:class:`StageRouter`.

Mirrors :class:`course_supporter.agents.methodist.MethodistAgent`: the
stage ladder + prompt live in ``config/ladders_mentor.yaml``
(``criteria_decomposition``) and ``prompts/criteria_decomposition/``;
schema/semantic violations raise :class:`StructuralRetryError` so the
router's instructor-style retry + fallback kicks in; ladder exhaustion
surfaces as :class:`LadderExhaustedError` for the caller to handle.

The ladder's prompt is ``v2.md`` (task 08); ``v1.md``, the
``{statement, evidence}`` pairs of the retired cache, stays in the tree for
the register rows that name it — the stage kept its name, and the register
tells the versions apart by ``prompt_ref``.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from course_supporter.config import get_settings
from course_supporter.homework.criteria_form import (
    MAX_CRITERIA,
    CriteriaComposition,
    CriterionDraft,
    check_methods_for,
    compose_criteria,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.stage_router import StageExecution

if TYPE_CHECKING:
    from course_supporter.llm.ladder_config import StageConfig
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

STAGE_NAME = "criteria_decomposition"


@lru_cache(maxsize=1)
def _ladder_stage() -> StageConfig:
    """The stage as ``config/ladders_*.yaml`` declares it, read once per process.

    The same files the router was built from at startup, so :meth:`compose`
    walks the router's own ladder — only the way down it differs.
    """
    return load_ladder_config(get_settings().ladders_dir).get_stage(STAGE_NAME)


class _CompositionResponse(BaseModel):
    """Strict shape of the v2 response: drafts and contradictions.

    ``contradictions`` defaults to empty for the reason the drafts' lists do
    (:class:`~course_supporter.homework.criteria_form.CriterionDraft`): leaving
    out an empty list says the same as ``[]``.
    """

    model_config = ConfigDict(extra="forbid")

    criteria: Annotated[
        tuple[CriterionDraft, ...], Field(min_length=1, max_length=MAX_CRITERIA)
    ]
    contradictions: tuple[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)], ...
    ] = ()


def _validation_feedback(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    logger.warning(
        "criteria_decomposition_validation_failed",
        error_type=first.get("type", "unknown"),
        error_msg=first.get("msg", ""),
        error_loc=loc,
    )
    return (
        f"{first.get('msg', 'validation error')} (field: {loc or '<root>'}). "
        "Regenerate the response with valid JSON matching the schema."
    )


class CriteriaDecomposerAgent:
    """Compose a task version's criteria list from the task in its course (D11)."""

    def __init__(
        self, stage_router: StageRouter, *, stage: StageConfig | None = None
    ) -> None:
        """Bind the agent to a router.

        Args:
            stage_router: The router the stage is walked on.
            stage: The stage :meth:`compose` walks; the ladders' own
                ``criteria_decomposition`` when omitted. Tests hand in a stage
                of their own; production never does.
        """
        self._stage_router = stage_router
        self._stage = stage

    def _stage_config(self) -> StageConfig:
        return self._stage if self._stage is not None else _ladder_stage()

    def prompt_hash(self) -> str:
        """The version of the prompt :meth:`compose` renders.

        The hash the call register stores next to ``prompt_ref`` — of the
        template, before rendering — so a list can say which prompt made it.
        """
        return load_prompt(self._stage_config().prompt_ref).content_hash()

    async def compose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
        node_description: str,
        node_concepts: Sequence[str],
        root_concepts: Sequence[str],
    ) -> CriteriaComposition:
        """Compose one task version's criteria list in the task-08 form (v2).

        The model reads the task in its course (``TASK.md`` 3.2) and writes
        drafts; the code checks them, then assigns the identifiers and drops
        every concept the input did not have
        (:func:`~course_supporter.homework.criteria_form.compose_criteria`).

        The stage is walked through an execution of this agent's own, with
        the stop at the output ceiling on (``TASK.md`` section 9, decision 4):
        a rung that spends its whole ceiling on an empty answer ends the walk
        instead of paying the next rung for the same empty answer. The
        by-name entry keeps descending for every other caller.

        Args:
            task_title: The task document's title.
            task_description: The task document's short description.
            task_text: The full task material.
            task_type: The assignment type; it steers the granularity and
                decides whether ``code_test`` is offered and accepted.
            language: Human-readable name of the course language for the
                free-text fields (``"Ukrainian"``, not ``"ukr"``); ``None``
                lets the model follow the task's own language.
            node_description: The description of the task's node, ``""``
                when there is none — contradictions are sought against it.
            node_concepts: The main concepts of the task's node.
            root_concepts: The main concepts of the course root.

        Returns:
            The composition: criteria, contradictions for the author, and the
            count of dropped concepts.

        Raises:
            LadderExhaustedError: The ladder could not produce a valid list;
                ``stop`` says whether it stopped at the output ceiling
                (propagated for the caller to handle).
        """
        admitted = check_methods_for(task_type)
        parsed: dict[str, _CompositionResponse] = {}

        def _validator(content: str) -> None:
            try:
                response = _CompositionResponse.model_validate_json(content)
            except ValidationError as exc:
                raise StructuralRetryError(_validation_feedback(exc)) from exc
            for index, draft in enumerate(response.criteria):
                if draft.check_method not in admitted:
                    logger.warning(
                        "criteria_decomposition_validation_failed",
                        error_type="check_method_not_admitted",
                        error_msg=draft.check_method.value,
                        error_loc=f"criteria.{index}.check_method",
                    )
                    raise StructuralRetryError(
                        f"criteria.{index}.check_method is "
                        f"{draft.check_method.value!r}, which a {task_type!r} "
                        "assignment does not admit; use one of "
                        f"{sorted(method.value for method in admitted)}. "
                        "Regenerate the response."
                    )
            parsed["response"] = response

        execution = StageExecution(
            stage=self._stage_config(),
            stage_name=STAGE_NAME,
            stop_on_output_ceiling=True,
        )
        await execution.run(
            self._stage_router,
            response_validator=_validator,
            expects_json=True,
            task_type=task_type,
            language=language,
            task_title=task_title,
            task_description=task_description,
            task_text=task_text,
            node_description=node_description,
            node_concepts=list(node_concepts),
            root_concepts=list(root_concepts),
        )

        response = parsed["response"]
        return compose_criteria(
            response.criteria,
            response.contradictions,
            input_concepts=[*node_concepts, *root_concepts],
        )
