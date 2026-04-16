"""MacroTOCAgent: Stage-5 LLM call that splits text into macro sections.

Takes the raw ``processed_content`` Markdown and asks the LLM for a
flat, ordered list of thematic macro sections — each described by a
``title`` plus a verbatim ``start_snippet``. The orchestrator resolves
snippets to exact character offsets via substring search; the LLM is
never trusted to count characters.

Used for ``text`` and ``web`` source types (identical Stage-5 logic).
Per-source processors (video, audio, presentation) will introduce
their own agents in follow-up PRs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import structlog

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
from course_supporter.models.macro_segment import MacroSectionsLLMOutput

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.schemas import LLMResponse

logger = structlog.get_logger()

MACRO_TOC_PROMPT_PATH = "prompts/macro_toc/v1_text.yaml"


class MacroTOCResult(NamedTuple):
    """Stage-5 LLM output bundled with prompt version + response metadata."""

    output: MacroSectionsLLMOutput
    prompt_version: str
    response: LLMResponse


class MacroTOCAgent:
    """Calls the LLM to produce a macro-section table of contents.

    Args:
        router: ModelRouter instance used for LLM calls.
        strategy: Routing strategy (``default`` / ``quality`` / ``budget``).
        temperature: Sampling temperature. 0.0 for deterministic boundaries.
        max_tokens: Optional output token cap. Output is small (titles
            + short snippets) so default None is usually fine.
        prompt_path: Override for the YAML prompt file (mainly tests).
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        strategy: str = "budget",
        temperature: float = 0.0,
        max_tokens: int | None = None,
        prompt_path: str | None = None,
    ) -> None:
        self._router = router
        self._strategy = strategy
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt_path = prompt_path or MACRO_TOC_PROMPT_PATH

    async def run(self, *, processed_content: str) -> MacroTOCResult:
        """Run the Stage-5 LLM call on ``processed_content``.

        Returns the raw LLM output plus prompt version and LLM response
        metadata (model, tokens, cost) for ExternalServiceCall logging.
        """
        prompt_data = load_prompt(self._prompt_path)
        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            processed_content,
        )

        logger.info(
            "macro_toc_agent_generating",
            strategy=self._strategy,
            prompt_version=prompt_data.version,
            content_chars=len(processed_content),
        )

        result, response = await self._router.complete_structured(
            action="content_macro_toc",
            prompt=user_prompt,
            response_schema=MacroSectionsLLMOutput,
            system_prompt=prompt_data.system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            strategy=self._strategy,
        )
        output: MacroSectionsLLMOutput = result

        logger.info(
            "macro_toc_agent_done",
            sections_count=len(output.sections),
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return MacroTOCResult(
            output=output,
            prompt_version=prompt_data.version,
            response=response,
        )
