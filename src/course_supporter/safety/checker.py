"""Safety checker orchestrator: regex scan → LLM classification."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from course_supporter.config import get_settings
from course_supporter.models.safety import (
    CourseContext,
    SafetyResult,
    SafetyVerdict,
    SubmissionContent,
)
from course_supporter.safety.archive import extract_submission_content
from course_supporter.safety.patterns import has_critical_matches, scan_for_injections

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter

logger = structlog.get_logger()

_PROMPT_PATH = "prompts/safety_check/v1.yaml"


class SafetyChecker:
    """Orchestrates the safety check pipeline.

    Pipeline:
    1. Extract text content (supports archives)
    2. Regex scan for known injection patterns (free, instant)
       → critical match = instant reject (no LLM call)
    3. LLM classification with course context (injection + relevance)
    4. Combine results into SafetyResult
    """

    async def check(
        self,
        file_path: Path,
        course_context: CourseContext,
        router: ModelRouter,
    ) -> SafetyResult:
        """Run the full safety check pipeline.

        Args:
            file_path: Path to the downloaded submission file.
            course_context: Course/node info for relevance checking.
            router: ModelRouter for LLM calls.

        Returns:
            SafetyResult with combined verdict.
        """
        # 1. Extract content
        content = await extract_submission_content(file_path)
        logger.info(
            "safety_content_extracted",
            files_count=len(content.files),
            total_size=content.total_size,
        )

        # 2. Regex scan
        full_text = content.full_text
        pattern_matches = scan_for_injections(full_text)

        if pattern_matches:
            logger.warning(
                "safety_patterns_detected",
                count=len(pattern_matches),
                patterns=[m.pattern_name for m in pattern_matches],
            )

        # Critical match → instant reject without LLM
        if has_critical_matches(pattern_matches):
            all_flags = list({m.pattern_name for m in pattern_matches})
            reason = (
                f"Prompt injection detected via pattern scan: {', '.join(all_flags)}"
            )
            return SafetyResult(
                safe=False,
                reason=reason,
                flags=all_flags,
                verdict=None,
                pattern_matches=pattern_matches,
            )

        # 3. LLM classification
        verdict = await self._llm_classify(content, course_context, router)
        logger.info(
            "safety_llm_verdict",
            safe=verdict.safe,
            confidence=verdict.confidence,
            flags=verdict.flags,
        )

        # 4. Combine
        all_flags = list({m.pattern_name for m in pattern_matches} | set(verdict.flags))
        safe = verdict.safe and not pattern_matches

        reason = verdict.reason
        if pattern_matches and verdict.safe:
            reason = (
                f"Pattern scan flagged potential issues: "
                f"{', '.join(m.pattern_name for m in pattern_matches)}. "
                f"LLM verdict: {verdict.reason}"
            )

        return SafetyResult(
            safe=safe,
            reason=reason,
            flags=all_flags,
            verdict=verdict,
            pattern_matches=pattern_matches,
        )

    async def _llm_classify(
        self,
        content: SubmissionContent,
        course_context: CourseContext,
        router: ModelRouter,
    ) -> SafetyVerdict:
        """Classify content safety via LLM structured output."""
        from course_supporter.agents.prompt_loader import (
            format_user_prompt,
            load_prompt,
        )

        settings = get_settings()
        max_chars = settings.safety_max_content_chars

        # Truncate content for LLM context
        full_text = content.full_text
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[... truncated ...]"

        # Load prompt template
        prompt_data = load_prompt(_PROMPT_PATH)

        # Build user prompt with course context
        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            full_text,
            course_title=course_context.course_title,
            course_description=course_context.course_description,
            node_title=course_context.node_title,
            node_description=course_context.node_description,
            outline_summary=course_context.outline_summary,
        )

        verdict_data, response = await router.complete_structured(
            action="safety_check",
            prompt=user_prompt,
            response_schema=SafetyVerdict,
            system_prompt=prompt_data.system_prompt,
            temperature=0.0,
        )

        logger.info(
            "safety_llm_call",
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return cast(SafetyVerdict, verdict_data)
