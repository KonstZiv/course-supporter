"""ArchitectAgent: generates course structure from materials via LLM."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NamedTuple

import structlog

if TYPE_CHECKING:
    from course_supporter.models.step import (
        ChildSnapshotContext,
        NodeSummary,
        StepInput,
        StepOutput,
    )

from course_supporter.agents.prompt_loader import (
    format_user_prompt,
    load_prompt,
    load_split_prompt,
)
from course_supporter.llm.router import ModelRouter
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseContext, CourseStructure

logger = structlog.get_logger()


class NodePosition(StrEnum):
    """Position of a node in the material tree hierarchy."""

    LEAF = "leaf"
    INTERMEDIATE = "intermediate"
    ROOT = "root"


# Legacy v1 prompt paths (single-file system+user).
V1_PROMPT_PATHS: dict[str, str] = {
    "free": "prompts/architect/v1.yaml",
    "guided": "prompts/architect/v1_guided.yaml",
}

# v2 prompt paths (split system + per-position user).
V2_SYSTEM_PATH = "prompts/architect/v2_system.yaml"
V2_USER_PATHS: dict[NodePosition, str] = {
    NodePosition.LEAF: "prompts/architect/v2_leaf.yaml",
    NodePosition.INTERMEDIATE: "prompts/architect/v2_intermediate.yaml",
    NodePosition.ROOT: "prompts/architect/v2_root.yaml",
}


class GenerationResult(NamedTuple):
    """Result of structure generation including LLM metadata.

    Bundles the validated structure, prompt version used, and full
    LLM response with token counts and cost for persistence.
    """

    structure: CourseStructure
    prompt_version: str
    response: LLMResponse


class PreparedPrompt(NamedTuple):
    """Intermediate result of prompt preparation step.

    Separates prompt loading/formatting from LLM invocation,
    enabling independent testing and future graph-based orchestration
    where each step becomes a node.
    """

    system_prompt: str
    user_prompt: str
    prompt_version: str


class ArchitectAgent:
    """Generates structured course program from course materials.

    Uses ModelRouter with action='course_structuring' to call LLM
    with structured output (CourseStructure Pydantic schema).

    Supports two prompt versions:
    - **v1** (legacy): single YAML file selected by mode (free/guided).
    - **v2**: split system prompt + per-position user prompt
      (leaf/intermediate/root).

    Args:
        router: ModelRouter instance for LLM calls.
        mode: Generation mode ('free' or 'guided'). Used by v1 prompts.
        node_position: Position in material tree. If provided, uses v2 prompts.
        prompt_path: Override path to YAML prompt template (ignores mode/position).
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature (0.0 = deterministic).
        max_tokens: Maximum output tokens.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        mode: Literal["free", "guided"] = "free",
        node_position: NodePosition | None = None,
        prompt_path: str | None = None,
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> None:
        self._router = router
        self._mode = mode
        self._node_position = node_position
        self._prompt_path = prompt_path
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(
        self,
        context: CourseContext,
        *,
        existing_structure: str | None = None,
    ) -> CourseStructure:
        """Generate course structure from materials.

        Orchestrates the pipeline: prepare prompts -> generate via LLM.

        Args:
            context: Unified course context from ingestion pipeline.
            existing_structure: Serialized existing tree for guided mode.

        Returns:
            Validated CourseStructure from LLM.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompts(context, existing_structure=existing_structure)
        structure, _response = await self._generate(
            prepared, documents_count=len(context.documents)
        )
        return structure

    async def run_with_metadata(
        self,
        context: CourseContext,
        *,
        existing_structure: str | None = None,
        children_context: str = "",
        children_snapshots: str = "",
    ) -> GenerationResult:
        """Generate course structure with full LLM metadata.

        Same pipeline as :meth:`run` but returns prompt version and
        LLM response (model_id, tokens, cost) for persistence.

        Args:
            context: Unified course context from ingestion pipeline.
            existing_structure: Serialized existing tree for guided mode.
            children_context: Formatted children summaries for per-node DAG.
            children_snapshots: Formatted child snapshots for context compression.

        Returns:
            GenerationResult with structure, prompt_version, and LLMResponse.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompts(
            context,
            existing_structure=existing_structure,
            children_context=children_context,
            children_snapshots=children_snapshots,
        )
        structure, response = await self._generate(
            prepared, documents_count=len(context.documents)
        )
        return GenerationResult(
            structure=structure,
            prompt_version=prepared.prompt_version,
            response=response,
        )

    def _prepare_prompts(
        self,
        context: CourseContext,
        *,
        existing_structure: str | None = None,
        children_context: str = "",
        children_snapshots: str = "",
    ) -> PreparedPrompt:
        """Step 1: Load prompt template and format with context.

        Selects prompt source based on configuration:
        - If ``prompt_path`` is set, uses that single file (override).
        - If ``node_position`` is set, uses v2 split prompts.
        - Otherwise falls back to v1 single-file prompts by mode.

        Args:
            context: Course context to serialize into the prompt.
            existing_structure: Serialized existing tree for guided mode.
            children_context: Formatted children summaries for per-node DAG.
            children_snapshots: Formatted child snapshots for context compression.

        Returns:
            PreparedPrompt with system prompt, formatted user prompt,
            and prompt version for logging/A/B testing.

        Raises:
            FileNotFoundError: If prompt YAML file not found.
            ValidationError: If YAML missing required keys.
        """
        if self._prompt_path:
            prompt_data = load_prompt(self._prompt_path)
        elif self._node_position is not None:
            prompt_data = load_split_prompt(
                V2_SYSTEM_PATH,
                V2_USER_PATHS[self._node_position],
            )
        else:
            prompt_data = load_prompt(V1_PROMPT_PATHS[self._mode])

        kwargs: dict[str, str] = {}
        if self._mode == "guided":
            kwargs["existing_structure"] = (
                existing_structure or "No existing structure provided."
            )
        kwargs["children_context"] = children_context
        kwargs["children_snapshots"] = children_snapshots

        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            context.model_dump_json(),
            **kwargs,
        )
        return PreparedPrompt(
            system_prompt=prompt_data.system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_data.version,
        )

    async def _generate(
        self,
        prepared: PreparedPrompt,
        *,
        documents_count: int = 0,
    ) -> tuple[CourseStructure, LLMResponse]:
        """Step 2: Call LLM and return validated CourseStructure with response.

        Sends prepared prompts to ModelRouter with structured output
        and returns the parsed Pydantic model alongside raw LLM response.

        Args:
            prepared: Formatted prompts from _prepare_prompts step.
            documents_count: Number of documents for logging.

        Returns:
            Tuple of (CourseStructure, LLMResponse).

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
        """
        from course_supporter.llm.router import estimate_tokens

        estimated = estimate_tokens(prepared.user_prompt, prepared.system_prompt)
        logger.info(
            "architect_agent_generating",
            strategy=self._strategy,
            prompt_version=prepared.prompt_version,
            documents_count=documents_count,
            context_chars=len(prepared.user_prompt),
            estimated_tokens=estimated,
            mode=self._mode,
            node_position=str(self._node_position) if self._node_position else None,
        )

        result, response = await self._router.complete_structured(
            action="course_structuring",
            prompt=prepared.user_prompt,
            response_schema=CourseStructure,
            system_prompt=prepared.system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )
        structure: CourseStructure = result

        logger.info(
            "architect_agent_done",
            modules_count=len(structure.modules),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return structure, response

    async def execute(self, step_input: StepInput) -> StepOutput:
        """Execute a generation step from StepInput contract.

        Bridges the StepInput/StepOutput contract with the existing
        run_with_metadata pipeline. Builds CourseContext from StepInput,
        delegates to run_with_metadata, extracts summary and concepts.

        Args:
            step_input: Immutable input assembled by Step Executor.

        Returns:
            StepOutput with structure, summary, concepts, and LLM metadata.
        """
        from course_supporter.ingestion.merge import MergeStep
        from course_supporter.models.step import StepOutput

        context = MergeStep().merge(
            step_input.materials,
            step_input.slide_timecode_refs or None,
            material_tree=step_input.material_tree or None,
        )
        children_context = _format_children_context(step_input.children_summaries)
        children_snapshots = _format_children_snapshots(step_input.children_snapshots)
        gen_result = await self.run_with_metadata(
            context,
            existing_structure=step_input.existing_structure,
            children_context=children_context,
            children_snapshots=children_snapshots,
        )
        return StepOutput(
            structure=gen_result.structure,
            summary=gen_result.structure.summary,
            core_concepts=gen_result.structure.core_concepts,
            mentioned_concepts=gen_result.structure.mentioned_concepts,
            summary_nested_nodes=gen_result.structure.summary_nested_nodes,
            prompt_version=gen_result.prompt_version,
            response=gen_result.response,
        )


def _format_children_context(
    children_summaries: list[NodeSummary],
) -> str:
    """Format children summaries as text for the LLM prompt.

    Returns an empty string when there are no children summaries (leaf node).
    """
    if not children_summaries:
        return ""

    lines = ["## Children Summaries", ""]
    for cs in children_summaries:
        lines.append(f"### {cs.title}")
        lines.append(f"**Summary:** {cs.summary}")
        if cs.core_concepts:
            lines.append(f"**Core concepts:** {', '.join(cs.core_concepts)}")
        if cs.mentioned_concepts:
            lines.append(f"**Mentioned concepts:** {', '.join(cs.mentioned_concepts)}")
        lines.append("")
    return "\n".join(lines)


def _format_children_snapshots(
    children_snapshots: list[ChildSnapshotContext],
) -> str:
    """Format full child snapshots as structured context for the LLM prompt.

    Includes the complete CourseStructure JSON from each child node,
    providing richer context than _format_children_context (which only
    includes summary + concepts). Used for context compression: parent
    nodes receive child snapshots instead of raw descendant materials.

    Returns an empty string when there are no child snapshots (leaf node).
    """
    if not children_snapshots:
        return ""

    lines = ["## Child Node Snapshots", ""]
    for cs in children_snapshots:
        lines.append(f"### {cs.title}")
        if cs.summary:
            lines.append(f"**Summary:** {cs.summary}")
        if cs.core_concepts:
            lines.append(f"**Core concepts:** {', '.join(cs.core_concepts)}")
        if cs.mentioned_concepts:
            lines.append(f"**Mentioned concepts:** {', '.join(cs.mentioned_concepts)}")
        if cs.summary_nested_nodes:
            lines.append(f"**Nested nodes summary:** {cs.summary_nested_nodes}")
        lines.append("**Structure:**")
        lines.append(json.dumps(cs.structure, ensure_ascii=False, indent=2))
        lines.append("")
    return "\n".join(lines)
