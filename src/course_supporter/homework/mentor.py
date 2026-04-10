"""Mentor agent — two-step homework review pipeline.

Step 1 (analysis): Budget LLM produces structured MentorAnalysis.
Step 2 (humanize): Premium LLM transforms analysis into natural review text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import structlog

from course_supporter.agents.prompt_loader import load_prompt
from course_supporter.homework.mentor_context import MentorContext
from course_supporter.models.mentor import MentorAnalysis, MentorReview

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter

logger = structlog.get_logger()

_ANALYSIS_PROMPT_PATH = "prompts/mentor/analysis_v1.yaml"
_HUMANIZE_PROMPT_PATH = "prompts/mentor/humanize_v1.yaml"

# Max chars of submission content to include in analysis prompt
_MAX_SUBMISSION_CHARS = 80_000
# Max chars of code fragments to include in humanize prompt
_MAX_CODE_FRAGMENT_CHARS = 5_000
# Humanize output length bounds (log warning only, non-blocking)
_MIN_REVIEW_CHARS = 50
_MAX_REVIEW_CHARS = 5_000


def _adjust_analysis(analysis: MentorAnalysis) -> tuple[MentorAnalysis, list[str]]:
    """Apply deterministic consistency rules to LLM analysis output.

    Returns a (possibly corrected) analysis and a list of adjustments made.
    Rules are soft caps — they correct obvious contradictions but do not
    override the LLM's judgment on subjective aspects.
    """
    adjustments: list[str] = []
    updates: dict[str, object] = {}

    critical_count = sum(1 for i in analysis.issues if i.severity == "critical")

    # Rule 1: incorrect → cannot pass
    if analysis.correctness == "incorrect" and analysis.passed:
        updates["passed"] = False
        adjustments.append("passed→False (correctness is incorrect)")

    # Rule 2: correct + critical issues → demote correctness
    if analysis.correctness == "correct" and critical_count > 0:
        updates["correctness"] = "partially_correct"
        adjustments.append(
            f"correctness→partially_correct ({critical_count} critical issues)",
        )

    # Rule 3: incorrect → cap score at 40
    if analysis.correctness == "incorrect" and analysis.score > 40:
        adjustments.append(f"score {analysis.score}→40 (incorrect)")
        updates["score"] = 40

    # Rule 4: critical issues → cap score at 74
    elif critical_count > 0 and analysis.score > 74:
        adjustments.append(f"score {analysis.score}→74 (critical issues)")
        updates["score"] = 74

    # Rule 5: no issues + correct → floor score at 70
    elif (
        not analysis.issues
        and analysis.correctness == "correct"
        and analysis.score < 70
    ):
        adjustments.append(f"score {analysis.score}→70 (correct, no issues)")
        updates["score"] = 70

    if updates:
        analysis = analysis.model_copy(update=updates)

    return analysis, adjustments


def _build_analysis_user_prompt(ctx: MentorContext) -> str:
    """Build dynamic user prompt for analysis step.

    Includes only non-empty sections. More context = deeper review.
    """
    sections: list[str] = []

    # Task info (always present)
    sections.append(f"## Task: {ctx.task_title}")
    sections.append(f"- **Type:** {ctx.node_type}")

    if ctx.task_description:
        sections.append(f"- **Description:** {ctx.task_description}")
    if ctx.learning_goal:
        sections.append(f"- **Learning goal:** {ctx.learning_goal}")
    if ctx.difficulty:
        sections.append(f"- **Difficulty:** {ctx.difficulty}")
    if ctx.estimated_duration:
        sections.append(f"- **Expected duration:** {ctx.estimated_duration} min")

    # Evaluation criteria
    criteria_parts: list[str] = []
    if ctx.grading_criteria:
        criteria_parts.append(f"**Grading criteria:** {ctx.grading_criteria}")
    if ctx.success_criteria:
        criteria_parts.append(f"**Success criteria:** {ctx.success_criteria}")
    if ctx.assessment_method:
        criteria_parts.append(f"**Assessment method:** {ctx.assessment_method}")
    if criteria_parts:
        sections.append("\n## Evaluation Criteria")
        sections.extend(criteria_parts)

    # Assignment details from Methodist
    if ctx.assignment_description:
        sections.append(f"\n## Assignment Details\n{ctx.assignment_description}")
    if ctx.assignment_steps:
        steps_text = "\n".join(
            f"{i + 1}. {s}" for i, s in enumerate(ctx.assignment_steps)
        )
        sections.append(f"\n## Expected Steps\n{steps_text}")

    # Learning context
    if ctx.learning_objectives:
        obj_text = "\n".join(f"- {o}" for o in ctx.learning_objectives)
        sections.append(f"\n## Learning Objectives\n{obj_text}")
    if ctx.key_concepts:
        concepts_text = "\n".join(
            f"- **{c['name']}**: {c.get('definition', '')}" for c in ctx.key_concepts
        )
        sections.append(f"\n## Key Concepts\n{concepts_text}")

    # Common pitfalls
    pitfalls: list[str] = []
    pitfalls.extend(ctx.common_mistakes)
    pitfalls.extend(ctx.common_misconceptions)
    if pitfalls:
        pitfalls_text = "\n".join(f"- {p}" for p in pitfalls)
        sections.append(f"\n## Known Pitfalls (look for these)\n{pitfalls_text}")

    # Student history
    if ctx.student_history:
        history_parts: list[str] = []
        for h in ctx.student_history:
            line = f"- **{h.task_title}**: score={h.score}, passed={h.passed}"
            if h.issues_summary:
                line += f" | issues: {', '.join(h.issues_summary[:2])}"
            if h.notable_solutions_summary:
                line += f" | good: {', '.join(h.notable_solutions_summary[:2])}"
            history_parts.append(line)
        sections.append(
            "\n## Student History (previous submissions)\n" + "\n".join(history_parts)
        )

    # Course context
    if ctx.course_title:
        sections.append(f"\n## Course: {ctx.course_title}")
        if ctx.course_description:
            sections.append(ctx.course_description)

    # Submission content (the main payload)
    content = ctx.submission_content
    if len(content) > _MAX_SUBMISSION_CHARS:
        content = content[:_MAX_SUBMISSION_CHARS] + "\n\n[... truncated ...]"
    sections.append(f"\n## Student Submission\n```\n{content}\n```")

    return "\n".join(sections)


def _extract_code_fragments(ctx: MentorContext, analysis: MentorAnalysis) -> str:
    """Extract relevant code fragments for the humanize step.

    Collects fragments referenced in issues and notable_solutions,
    falls back to truncated full content.
    """
    fragments: list[str] = []

    for issue in analysis.issues:
        if issue.code_fragment:
            fragments.append(f"// Issue: {issue.description}\n{issue.code_fragment}")

    for ns in analysis.notable_solutions:
        if ns.code_fragment:
            fragments.append(f"// Good: {ns.description}\n{ns.code_fragment}")

    if fragments:
        result = "\n\n".join(fragments)
        return result[:_MAX_CODE_FRAGMENT_CHARS]

    # Fallback: first N chars of submission
    return ctx.submission_content[:_MAX_CODE_FRAGMENT_CHARS]


class MentorAgent:
    """Two-step homework review: analysis (budget LLM) + humanize (premium LLM)."""

    async def review(
        self,
        context: MentorContext,
        router: ModelRouter,
    ) -> MentorReview:
        """Run the full Mentor review pipeline.

        Args:
            context: Assembled MentorContext with all available data.
            router: ModelRouter for LLM calls.

        Returns:
            MentorReview with structured analysis and human-readable text.
        """
        log = logger.bind(
            task_title=context.task_title,
            language=context.response_language,
        )

        # Step 1: Analysis
        log.info("mentor_analysis_started")
        analysis = await self._analyze(context, router)
        log.info(
            "mentor_analysis_done",
            passed=analysis.passed,
            score=analysis.score,
            issues=len(analysis.issues),
            notable=len(analysis.notable_solutions),
        )

        # Step 2: Humanize
        log.info("mentor_humanize_started")
        review_text = await self._humanize(context, analysis, router)
        log.info("mentor_humanize_done", text_length=len(review_text))

        return MentorReview(
            analysis=analysis,
            review_text=review_text,
            response_language=context.response_language,
            reviewed_at=datetime.now(UTC),
        )

    async def _analyze(
        self,
        context: MentorContext,
        router: ModelRouter,
    ) -> MentorAnalysis:
        """Step 1: Produce structured analysis via budget LLM."""
        prompt_data = load_prompt(_ANALYSIS_PROMPT_PATH)
        user_prompt = _build_analysis_user_prompt(context)

        analysis_data, response = await router.complete_structured(
            action="homework_analysis",
            prompt=user_prompt,
            response_schema=MentorAnalysis,
            system_prompt=prompt_data.system_prompt,
            temperature=0.0,
        )

        logger.info(
            "mentor_analysis_llm_call",
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        raw_analysis = cast(MentorAnalysis, analysis_data)
        adjusted, adjustments = _adjust_analysis(raw_analysis)
        if adjustments:
            logger.warning(
                "mentor_analysis_adjusted",
                adjustments=adjustments,
            )
        return adjusted

    async def _humanize(
        self,
        context: MentorContext,
        analysis: MentorAnalysis,
        router: ModelRouter,
    ) -> str:
        """Step 2: Transform structured analysis into natural review text."""
        prompt_data = load_prompt(_HUMANIZE_PROMPT_PATH)

        analysis_json = analysis.model_dump_json(indent=2)
        code_fragments = _extract_code_fragments(context, analysis)

        user_prompt = prompt_data.user_prompt_template.format(
            analysis_json=analysis_json,
            code_fragments=code_fragments,
            response_language=context.response_language,
        )
        system_prompt = prompt_data.system_prompt.format(
            response_language=context.response_language,
        )

        # Humanize returns plain text, not structured output
        response = await router.complete(
            action="homework_humanize",
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
        )

        logger.info(
            "mentor_humanize_llm_call",
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        review_text = response.content
        text_len = len(review_text)
        if text_len < _MIN_REVIEW_CHARS:
            logger.warning("mentor_humanize_too_short", length=text_len)
        elif text_len > _MAX_REVIEW_CHARS:
            logger.warning("mentor_humanize_too_long", length=text_len)

        return review_text
