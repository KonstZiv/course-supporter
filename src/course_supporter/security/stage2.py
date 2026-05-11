"""Stage 2 LLM-backed safety classifier (vision §KD14, §KD16).

Runs after :func:`course_supporter.security.stage1.run_stage1` returns
successfully. Wires the ``safety_check`` stage of
:class:`course_supporter.llm.stage_router.StageRouter` (KD16) to the
:class:`course_supporter.security.schemas.SafetyResult` schema so the
caller (HTTP layer / homework processor) gets a typed verdict.

## Acceptance trade-off (sealed by 0.5 contract)

Per the 0.6 acceptance criterion (BLOCKER #1 resolution): when the
LLM responds successfully but its content does not parse to a valid
:class:`SafetyResult`, this orchestrator raises **terminal**
:class:`course_supporter.security.exceptions.SafetyValidationError`.
Structural retry against the same provider is intentionally NOT
implemented because the 0.5 ``StageRouter`` contract is sealed and
its ``StructuralRetryError`` hook only fires when the *provider*
raises -- a successful response with broken JSON does not trigger
the retry path.

The deferred Phase 1+ extension (vision-side debt) is to add a
``response_validator`` callback to ``StageRouter`` that schema-checks
content; once landed, this module can convert
:class:`SafetyValidationError` into a router-driven retry. Until
then, terminal failure is the contract.

## Context-variable contract (caller responsibility)

This module mirrors :class:`StageRouter`'s ContextVar discipline
exactly -- there is no ``job_id`` parameter. The caller MUST set
both ContextVars before invocation:

* ``_current_job_id`` via :func:`service_logging.job_scope` or
  :func:`service_logging.set_job_from_arq`.
* ``_current_tenant_id`` via :func:`service_logging.tenant_scope`
  or :func:`service_logging.set_tenant_from_job`.

When either is unset, :func:`service_logging._persist` skips the
ESC write with a WARNING and the audit chain is broken downstream.
The orchestrator does not hard-fail in that case (matches
``StageRouter`` semantics) but the WARNING is the signal to the
caller that the wiring is incomplete.

## What this module does NOT decide

Acting on the verdict (e.g. rejecting an ``is_safe=False``
submission, persisting the result to ``HomeworkSubmission.safety_result``
JSONB) is the caller's responsibility. This orchestrator only
parses; downstream business policy lives elsewhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from course_supporter.security.exceptions import SafetyValidationError
from course_supporter.security.schemas import SafetyResult
from course_supporter.service_logging import get_current_job_id

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.security.schemas import CourseContext

logger = structlog.get_logger(__name__)


# Truncate raw LLM content in error logs to avoid dumping full
# submission echoes / hallucinated payloads into the log pipeline.
# 200 chars is enough to spot the pattern (markdown fence, leading
# preamble, trailing prose) without bloating log records.
_PARSE_ERROR_PREVIEW_CHARS = 200


async def run_stage2_safety_check(
    submission_text: str,
    *,
    router: StageRouter,
    course_context: CourseContext | None = None,
) -> SafetyResult:
    """Execute the ``safety_check`` stage and parse the verdict.

    Caller responsibilities:

    * ``submission_text`` MUST already be NFC-normalized (Stage 1
      contract per :func:`course_supporter.security.stage1.run_stage1`).
      This orchestrator does NOT re-normalize -- doing so would
      diverge the audit text from what the LLM actually classified.
    * ``_current_job_id`` and ``_current_tenant_id`` ContextVars
      MUST be set per the module docstring. Both are required by
      :class:`StageRouter` for ESC persistence; this layer reads
      ``_current_job_id`` for log correlation only and never
      independently sets either.

    Args:
        submission_text: NFC-normalized text from Stage 1, suitable
            for direct insertion into the ``{{ submission_text }}``
            Jinja placeholder of ``prompts/safety_check/v1.md``.
        router: Configured :class:`StageRouter` (KD16). Production
            wiring constructs one router per app and reuses it; tests
            inject a mocked-provider router via this dependency
            injection point.
        course_context: Optional course/topic context. When provided,
            its fields populate the conditional ``Course context``
            block of the v1.md prompt, sharpening the ``off_topic``
            judgement against the active course. ``None`` (default)
            renders the prompt without that block — generic safety
            classification appropriate for non-homework callers
            (Phase 2.1 authored-pipeline integration). KD-1.2-I:
            preserves legacy course-aware safety check behavior
            (migrated from removed ``SafetyChecker`` to this
            canonical orchestrator) without forcing the same shape
            onto every Stage 2 caller.

    Returns:
        Parsed :class:`SafetyResult` (typed Pydantic model). The
        caller decides downstream policy on ``is_safe=False``.

    Raises:
        SafetyValidationError: terminal -- the LLM responded
            successfully but its content does not parse. No ladder
            reuse / retry per 0.6 acceptance.
        course_supporter.llm.error_categories.LadderExhaustedError:
            propagated unchanged when every ladder entry failed at
            transport level.
    """
    job_id = get_current_job_id()

    logger.info(
        "stage2.safety_check.start",
        text_length=len(submission_text),
        course_context_provided=course_context is not None,
        job_id=str(job_id) if job_id is not None else None,
    )

    # Always pass course-context vars (empty strings when absent) so
    # the v1.md template's StrictUndefined Jinja2 environment does not
    # raise UndefinedError; the template uses ``{% if course_title %}``
    # to gate the entire course-context block on truthiness.
    render_context: dict[str, str] = {
        "submission_text": submission_text,
        "course_title": course_context.course_title if course_context else "",
        "course_description": (
            course_context.course_description if course_context else ""
        ),
        "node_title": course_context.node_title if course_context else "",
        "node_description": (course_context.node_description if course_context else ""),
        "outline_summary": (course_context.outline_summary if course_context else ""),
    }

    result = await router.execute_for_stage(
        "safety_check",
        **render_context,
    )

    try:
        parsed = SafetyResult.model_validate_json(result.content)
    except ValidationError as exc:
        logger.error(
            "stage2.safety_check.parse_failed",
            provider=result.provider_used,
            model=result.model_used,
            attempt_count=result.attempt_count,
            content_preview=result.content[:_PARSE_ERROR_PREVIEW_CHARS],
            validation_error=str(exc),
            job_id=str(job_id) if job_id is not None else None,
        )
        raise SafetyValidationError(
            raw_content=result.content,
            parse_error=str(exc),
        ) from exc

    logger.info(
        "stage2.safety_check.success",
        provider=result.provider_used,
        model=result.model_used,
        attempt_count=result.attempt_count,
        is_safe=parsed.is_safe,
        violations=[v.value for v in parsed.violations],
        confidence=parsed.confidence,
        job_id=str(job_id) if job_id is not None else None,
    )
    return parsed
