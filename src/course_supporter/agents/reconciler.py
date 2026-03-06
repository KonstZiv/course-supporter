"""ReconcileAgent: detects cross-node inconsistencies via LLM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from course_supporter.models.step import NodeSummary, StepInput, StepOutput

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.llm.router import ModelRouter
from course_supporter.models.course import CourseStructure

logger = structlog.get_logger()

RECONCILE_PROMPT_PATH = "prompts/architect/v1_reconcile.yaml"


class ReconcileAgent:
    """Detects and fixes cross-node inconsistencies in course structures.

    Uses a sliding window of context (parent, siblings, children) to
    identify terminology mismatches, coverage gaps, and ordering issues.

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
        """Execute a reconciliation step from StepInput contract.

        Builds sliding window context from StepInput, calls LLM with
        reconciliation prompt, returns reconciled structure.

        Args:
            step_input: Immutable input assembled by Step Executor.

        Returns:
            StepOutput with reconciled structure and LLM metadata.
        """
        from course_supporter.ingestion.merge import MergeStep
        from course_supporter.models.step import StepOutput

        context = MergeStep().merge(
            step_input.materials,
            step_input.slide_timecode_refs or None,
            material_tree=step_input.material_tree or None,
        )

        parent_ctx = _format_parent_context(step_input.parent_context)
        sibling_ctx = _format_sibling_context(step_input.sibling_summaries)
        children_ctx = _format_children_context(step_input.children_summaries)

        prompt_data = load_prompt(RECONCILE_PROMPT_PATH)
        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            context.model_dump_json(),
            parent_context=parent_ctx,
            sibling_context=sibling_ctx,
            children_context=children_ctx,
        )

        logger.info(
            "reconcile_agent_generating",
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
            "reconcile_agent_done",
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


def _format_parent_context(parent: NodeSummary | None) -> str:
    """Format parent node summary for the reconciliation prompt."""
    if parent is None:
        return ""

    lines = [
        "## Parent Context",
        "",
        f"**Title:** {parent.title}",
        f"**Summary:** {parent.summary}",
    ]
    if parent.core_concepts:
        lines.append(f"**Core concepts:** {', '.join(parent.core_concepts)}")
    if parent.mentioned_concepts:
        lines.append(f"**Mentioned concepts:** {', '.join(parent.mentioned_concepts)}")
    lines.append("")
    return "\n".join(lines)


def _format_sibling_context(
    siblings: list[NodeSummary],
) -> str:
    """Format sibling node summaries for the reconciliation prompt."""
    if not siblings:
        return ""

    lines = ["## Sibling Summaries", ""]
    for sib in siblings:
        lines.append(f"### {sib.title}")
        lines.append(f"**Summary:** {sib.summary}")
        if sib.core_concepts:
            lines.append(f"**Core concepts:** {', '.join(sib.core_concepts)}")
        if sib.mentioned_concepts:
            lines.append(f"**Mentioned concepts:** {', '.join(sib.mentioned_concepts)}")
        lines.append("")
    return "\n".join(lines)


def _format_children_context(
    children: list[NodeSummary],
) -> str:
    """Format children node summaries for the reconciliation prompt."""
    if not children:
        return ""

    lines = ["## Children Summaries", ""]
    for child in children:
        lines.append(f"### {child.title}")
        lines.append(f"**Summary:** {child.summary}")
        if child.core_concepts:
            lines.append(f"**Core concepts:** {', '.join(child.core_concepts)}")
        if child.mentioned_concepts:
            lines.append(
                f"**Mentioned concepts:** {', '.join(child.mentioned_concepts)}"
            )
        lines.append("")
    return "\n".join(lines)
