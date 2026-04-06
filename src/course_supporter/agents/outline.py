"""OutlineAgent: generates structured lossless outline from source material."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import structlog

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.models.outline import MaterialOutline

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.schemas import LLMResponse
    from course_supporter.models.source import SourceDocument

logger = structlog.get_logger()

OUTLINE_PROMPT_PATH = "prompts/outline/v1.yaml"


class OutlineResult(NamedTuple):
    """Result of outline generation including LLM metadata.

    Bundles the validated outline, prompt version used, and full
    LLM response with token counts and cost for persistence.
    """

    outline: MaterialOutline
    prompt_version: str
    response: LLMResponse


class PreparedOutlinePrompt(NamedTuple):
    """Intermediate result of prompt preparation step."""

    system_prompt: str
    user_prompt: str
    prompt_version: str


class OutlineAgent:
    """Generates a structured MaterialOutline from a single SourceDocument.

    Performs lossless restructuring of raw ingestion output (STT + VD chunks)
    into a navigable outline with thematic sections. Uses ModelRouter with
    action='material_outline' for LLM calls.

    Args:
        router: ModelRouter instance for LLM calls.
        strategy: Routing strategy ('default', 'quality', 'budget').
        temperature: LLM temperature (0.0 = deterministic).
        max_tokens: Maximum output tokens.
        prompt_path: Override path to YAML prompt template.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        strategy: str = "default",
        temperature: float = 0.0,
        max_tokens: int | None = None,
        prompt_path: str | None = None,
    ) -> None:
        self._router = router
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt_path = prompt_path or OUTLINE_PROMPT_PATH

    async def run(self, source_doc: SourceDocument) -> MaterialOutline:
        """Generate outline from source document.

        Orchestrates the pipeline: prepare prompt -> generate via LLM.

        Args:
            source_doc: Raw SourceDocument from ingestion pipeline.

        Returns:
            Validated MaterialOutline.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompt(source_doc)
        outline, _response = await self._generate(
            prepared, chunks_count=len(source_doc.chunks)
        )
        return outline

    async def run_with_metadata(self, source_doc: SourceDocument) -> OutlineResult:
        """Generate outline with full LLM metadata.

        Same pipeline as :meth:`run` but returns prompt version and
        LLM response (model_id, tokens, cost) for persistence.

        Args:
            source_doc: Raw SourceDocument from ingestion pipeline.

        Returns:
            OutlineResult with outline, prompt_version, and LLM response.

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
            FileNotFoundError: If prompt file not found.
        """
        prepared = self._prepare_prompt(source_doc)
        outline, response = await self._generate(
            prepared, chunks_count=len(source_doc.chunks)
        )
        return OutlineResult(
            outline=outline,
            prompt_version=prepared.prompt_version,
            response=response,
        )

    def _prepare_prompt(self, source_doc: SourceDocument) -> PreparedOutlinePrompt:
        """Step 1: Load prompt template and format with source document.

        Args:
            source_doc: Source document to serialize into the prompt.

        Returns:
            PreparedOutlinePrompt with system prompt, formatted user prompt,
            and prompt version.

        Raises:
            FileNotFoundError: If prompt YAML file not found.
            ValidationError: If YAML missing required keys.
        """
        prompt_data = load_prompt(self._prompt_path)
        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            source_doc.model_dump_json(),
        )
        return PreparedOutlinePrompt(
            system_prompt=prompt_data.system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_data.version,
        )

    async def _generate(
        self,
        prepared: PreparedOutlinePrompt,
        *,
        chunks_count: int = 0,
    ) -> tuple[MaterialOutline, LLMResponse]:
        """Step 2: Call LLM and return validated MaterialOutline with response.

        Args:
            prepared: Formatted prompts from _prepare_prompt step.
            chunks_count: Number of source chunks for logging.

        Returns:
            Tuple of (MaterialOutline, LLMResponse).

        Raises:
            AllModelsFailedError: If all models in all strategies fail.
        """
        from course_supporter.llm.router import estimate_tokens

        estimated = estimate_tokens(prepared.user_prompt, prepared.system_prompt)
        logger.info(
            "outline_agent_generating",
            strategy=self._strategy,
            prompt_version=prepared.prompt_version,
            chunks_count=chunks_count,
            context_chars=len(prepared.user_prompt),
            estimated_tokens=estimated,
        )

        result, response = await self._router.complete_structured(
            action="material_outline",
            prompt=prepared.user_prompt,
            response_schema=MaterialOutline,
            system_prompt=prepared.system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )
        outline: MaterialOutline = result

        logger.info(
            "outline_agent_done",
            sections_count=len(outline.sections),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return outline, response
