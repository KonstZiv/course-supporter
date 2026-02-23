"""ArchitectAgent: generates course structure from materials via LLM."""

from __future__ import annotations

from typing import Literal, NamedTuple

import structlog

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.llm.router import ModelRouter
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.course import CourseContext, CourseStructure

logger = structlog.get_logger()

PROMPT_PATHS: dict[str, str] = {
    "free": "prompts/architect/v1.yaml",
    "guided": "prompts/architect/v1_guided.yaml",
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

    Architecture: step-based design for future chain/graph migration.
    Each step is a separate method that can become a node in a
    LangGraph or custom DAG pipeline.

    Steps:
        1. _prepare_prompts: load YAML template, serialize context, format prompt
        2. _generate: call LLM via ModelRouter, return validated structure

    Args:
        router: ModelRouter instance for LLM calls.
        mode: Generation mode ('free' or 'guided').
        prompt_path: Override path to YAML prompt template (ignores mode).
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature (0.0 = deterministic).
        max_tokens: Maximum output tokens.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        mode: Literal["free", "guided"] = "free",
        prompt_path: str | None = None,
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> None:
        self._router = router
        self._mode = mode
        self._prompt_path = prompt_path or PROMPT_PATHS[mode]
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
    ) -> GenerationResult:
        """Generate course structure with full LLM metadata.

        Same pipeline as :meth:`run` but returns prompt version and
        LLM response (model_id, tokens, cost) for persistence.

        Args:
            context: Unified course context from ingestion pipeline.
            existing_structure: Serialized existing tree for guided mode.

        Returns:
            GenerationResult with structure, prompt_version, and LLMResponse.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompts(context, existing_structure=existing_structure)
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
    ) -> PreparedPrompt:
        """Step 1: Load prompt template and format with context.

        Loads YAML prompt file, serializes CourseContext to JSON,
        and injects it into the user prompt template. For guided mode,
        also injects the existing structure.

        Args:
            context: Course context to serialize into the prompt.
            existing_structure: Serialized existing tree for guided mode.

        Returns:
            PreparedPrompt with system prompt, formatted user prompt,
            and prompt version for logging/A/B testing.

        Raises:
            FileNotFoundError: If prompt YAML file not found.
            ValidationError: If YAML missing required keys.
        """
        prompt_data = load_prompt(self._prompt_path)

        kwargs: dict[str, str] = {}
        if existing_structure is not None:
            kwargs["existing_structure"] = existing_structure

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
        logger.info(
            "architect_agent_generating",
            strategy=self._strategy,
            prompt_version=prepared.prompt_version,
            documents_count=documents_count,
            context_chars=len(prepared.user_prompt),
            mode=self._mode,
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
