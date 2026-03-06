"""RefineAgent: preserves manual edits while harmonizing with context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from course_supporter.models.step import StepInput

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.agents.reconciler import (
    _format_children_context,
    _format_parent_context,
    _format_sibling_context,
)
from course_supporter.ingestion.merge import MergeStep
from course_supporter.llm.router import ModelRouter
from course_supporter.models.course import CourseStructure
from course_supporter.models.step import StepOutput

logger = structlog.get_logger()

REFINE_PROMPT_PATH = "prompts/refine/v1.yaml"


class RefineAgent:
    """Refines manually edited course structures via LLM.

    Preserves user edits while harmonizing with parent/sibling context
    and filling in missing details for skeleton items.

    Args:
        router: ModelRouter instance for LLM calls.
        mode: Generation mode ('free' or 'guided').
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature.
        max_tokens: Maximum output tokens.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        mode: Literal["free", "guided"] = "free",
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> None:
        self._router = router
        self._mode = mode
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def execute(self, step_input: StepInput) -> StepOutput:
        """Execute a refine step from StepInput contract.

        Builds prompt with existing (edited) structure and sliding window
        context, calls LLM, returns refined structure.

        Args:
            step_input: Immutable input assembled by Step Executor.

        Returns:
            StepOutput with refined structure and LLM metadata.
        """
        context = MergeStep().merge(
            step_input.materials,
            step_input.slide_timecode_refs or None,
            material_tree=step_input.material_tree or None,
        )

        parent_ctx = _format_parent_context(step_input.parent_context)
        sibling_ctx = _format_sibling_context(step_input.sibling_summaries)
        children_ctx = _format_children_context(step_input.children_summaries)

        prompt_data = load_prompt(REFINE_PROMPT_PATH)

        kwargs: dict[str, str] = {
            "existing_structure": (
                step_input.existing_structure or "No existing structure provided."
            ),
            "parent_context": parent_ctx,
            "sibling_context": sibling_ctx,
            "children_context": children_ctx,
        }
        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            context.model_dump_json(),
            **kwargs,
        )

        logger.info(
            "refine_agent_generating",
            strategy=self._strategy,
            prompt_version=prompt_data.version,
            context_chars=len(user_prompt),
            mode=self._mode,
        )

        structure, response = await self._router.complete_structured(
            action="course_structuring",
            prompt=user_prompt,
            response_schema=CourseStructure,
            system_prompt=prompt_data.system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )

        logger.info(
            "refine_agent_done",
            modules_count=len(structure.modules),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return StepOutput(
            structure=structure,
            summary=structure.summary,
            core_concepts=structure.core_concepts,
            mentioned_concepts=structure.mentioned_concepts,
            prompt_version=prompt_data.version,
            response=response,
        )
