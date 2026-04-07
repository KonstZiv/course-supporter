"""MethodistAgent: generates detailed methodological materials per node."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NamedTuple

import structlog

from course_supporter.agents.prompt_loader import (
    format_user_prompt,
    load_split_prompt,
)
from course_supporter.models.methodist import MethodistNodeOutput

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.schemas import LLMResponse
    from course_supporter.models.step import NodeSummary

logger = structlog.get_logger()

# Prompt paths (split: shared system + per-position user)
METHODIST_SYSTEM_PATH = "prompts/methodist/v1_system.yaml"
METHODIST_USER_PATHS: dict[str, str] = {
    "leaf": "prompts/methodist/v1_leaf.yaml",
    "intermediate": "prompts/methodist/v1_intermediate.yaml",
    "root": "prompts/methodist/v1_root.yaml",
}


class MethodistResult(NamedTuple):
    """Result of Methodist generation including LLM metadata."""

    output: MethodistNodeOutput
    prompt_version: str
    response: LLMResponse


class PreparedMethodistPrompt(NamedTuple):
    """Intermediate result of prompt preparation step."""

    system_prompt: str
    user_prompt: str
    prompt_version: str


NodePosition = Literal["leaf", "intermediate", "root"]


class MethodistAgent:
    """Generates detailed methodological materials for a course node.

    Uses a sliding window of context (parent, siblings, children)
    to produce per-node methodological documents with gap analysis,
    contradiction detection, and assignment recommendations.

    Args:
        router: ModelRouter instance for LLM calls.
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature (0.0 = deterministic).
        max_tokens: Maximum output tokens.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> None:
        self._router = router
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(
        self,
        *,
        node_title: str,
        node_description: str,
        node_type: str,
        node_position: NodePosition,
        outline_context: str,
        structure_context: str,
        parent_context: str = "",
        sibling_context: str = "",
        children_context: str = "",
        material_roles: str = "",
    ) -> MethodistNodeOutput:
        """Generate methodological materials for a node.

        Args:
            node_title: Title of the target node.
            node_description: Description of the target node.
            node_type: Type (module, lesson, concept, exercise).
            node_position: Position in tree (leaf/intermediate/root).
            outline_context: Layer 2 outlines JSON for this node.
            structure_context: Current structure metadata JSON.
            parent_context: Formatted parent node summary.
            sibling_context: Formatted sibling summaries.
            children_context: Formatted children summaries.
            material_roles: Info about educational vs methodological materials.

        Returns:
            Validated MethodistNodeOutput.
        """
        result = await self.run_with_metadata(
            node_title=node_title,
            node_description=node_description,
            node_type=node_type,
            node_position=node_position,
            outline_context=outline_context,
            structure_context=structure_context,
            parent_context=parent_context,
            sibling_context=sibling_context,
            children_context=children_context,
            material_roles=material_roles,
        )
        return result.output

    async def run_with_metadata(
        self,
        *,
        node_title: str,
        node_description: str,
        node_type: str,
        node_position: NodePosition,
        outline_context: str,
        structure_context: str,
        parent_context: str = "",
        sibling_context: str = "",
        children_context: str = "",
        material_roles: str = "",
    ) -> MethodistResult:
        """Generate methodological materials with LLM metadata.

        Same as :meth:`run` but returns prompt version and LLM
        response for persistence.
        """
        prepared = self._prepare_prompts(
            node_title=node_title,
            node_description=node_description,
            node_type=node_type,
            node_position=node_position,
            outline_context=outline_context,
            structure_context=structure_context,
            parent_context=parent_context,
            sibling_context=sibling_context,
            children_context=children_context,
            material_roles=material_roles,
        )
        output, response = await self._generate(prepared)
        return MethodistResult(
            output=output,
            prompt_version=prepared.prompt_version,
            response=response,
        )

    def _prepare_prompts(
        self,
        *,
        node_title: str,
        node_description: str,
        node_type: str,
        node_position: NodePosition,
        outline_context: str,
        structure_context: str,
        parent_context: str,
        sibling_context: str,
        children_context: str,
        material_roles: str,
    ) -> PreparedMethodistPrompt:
        """Load and format prompt templates for the given node position."""
        user_path = METHODIST_USER_PATHS[node_position]
        prompt_data = load_split_prompt(
            METHODIST_SYSTEM_PATH,
            user_path,
        )

        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            outline_context,
            node_title=node_title,
            node_description=node_description or "",
            node_type=node_type,
            structure_context=structure_context,
            parent_context=parent_context,
            sibling_context=sibling_context,
            children_context=children_context,
            material_roles=material_roles,
        )
        return PreparedMethodistPrompt(
            system_prompt=prompt_data.system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_data.version,
        )

    async def _generate(
        self,
        prepared: PreparedMethodistPrompt,
    ) -> tuple[MethodistNodeOutput, LLMResponse]:
        """Call LLM and return validated MethodistNodeOutput."""
        from course_supporter.llm.router import estimate_tokens

        estimated = estimate_tokens(
            prepared.user_prompt,
            prepared.system_prompt,
        )
        logger.info(
            "methodist_agent_generating",
            strategy=self._strategy,
            prompt_version=prepared.prompt_version,
            context_chars=len(prepared.user_prompt),
            estimated_tokens=estimated,
        )

        result, response = await self._router.complete_structured(
            action="methodist",
            prompt=prepared.user_prompt,
            response_schema=MethodistNodeOutput,
            system_prompt=prepared.system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )
        output: MethodistNodeOutput = result

        logger.info(
            "methodist_agent_done",
            objectives=len(output.learning_objectives),
            concepts=len(output.key_concepts_detailed),
            assignments=len(output.recommended_assignments),
            gaps=len(output.gaps),
            contradictions=len(output.contradictions),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return output, response


def format_methodist_children(
    children: list[NodeSummary],
) -> str:
    """Format children summaries for the Methodist prompt."""
    if not children:
        return ""
    lines = ["## Children Summaries", ""]
    for c in children:
        lines.append(f"### {c.title}")
        lines.append(f"**Summary:** {c.summary}")
        if c.core_concepts:
            concepts = ", ".join(c.core_concepts)
            lines.append(f"**Core concepts:** {concepts}")
        lines.append("")
    return "\n".join(lines)


def format_methodist_siblings(
    siblings: list[NodeSummary],
) -> str:
    """Format sibling summaries for the Methodist prompt."""
    if not siblings:
        return ""
    lines = ["## Sibling Summaries", ""]
    for s in siblings:
        lines.append(f"### {s.title}")
        lines.append(f"**Summary:** {s.summary}")
        if s.core_concepts:
            concepts = ", ".join(s.core_concepts)
            lines.append(f"**Core concepts:** {concepts}")
        lines.append("")
    return "\n".join(lines)


def format_methodist_parent(
    parent: NodeSummary | None,
) -> str:
    """Format parent node summary for the Methodist prompt."""
    if parent is None:
        return ""
    lines = [
        "## Parent Context",
        "",
        f"**Title:** {parent.title}",
        f"**Summary:** {parent.summary}",
    ]
    if parent.core_concepts:
        concepts = ", ".join(parent.core_concepts)
        lines.append(f"**Core concepts:** {concepts}")
    lines.append("")
    return "\n".join(lines)


def format_material_roles(
    entries: list[tuple[str, str, str]],
) -> str:
    """Format material role info for the Methodist prompt.

    Args:
        entries: List of (title, source_type, material_role) tuples.
    """
    if not entries:
        return ""
    lines = ["## Material Roles", ""]
    for title, stype, role in entries:
        lines.append(f"- **{title}** ({stype}): {role}")
    lines.append("")
    return "\n".join(lines)
