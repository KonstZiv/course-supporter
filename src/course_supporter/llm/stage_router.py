"""Stage-keyed LLM router (KD16).

Coexistent with the legacy :class:`course_supporter.llm.router.ModelRouter`,
not integrated. Phase 1+ tasks adopt :class:`StageRouter` as they
rewrite each agent; the legacy router stays in place until Phase 5.

Per-attempt fallback policy (vision §3 KD16):

* ``INFRASTRUCTURE`` -- exponential backoff retry on the same model
  (1 + ``max_retries_infrastructure`` total attempts; default 4),
  then fallback to the next ladder entry.
* ``STRUCTURAL`` -- single instructor-style retry with the parser
  feedback appended to the user prompt, then fallback. 0.5 ships
  the plumbing; Phase 1+ tasks add response validators that raise
  :class:`StructuralRetryError` after schema checks.
* ``SEMANTIC`` (any unrecognised exception) -- immediate fallback.
* ``INPUT_OVERFLOW`` -- immediate fallback. If every ladder entry
  is exhausted, raises :class:`LadderExhaustedError` with a
  per-attempt ``(provider, model, reason)`` breakdown.

Each attempt -- successful or not -- writes an ExternalServiceCall
row via :func:`service_logging._persist` so cost reporting can
correlate fallback chains. ``strategy="default"`` per KD16; the
ESC ``strategy`` column survives only for audit.

Limitations carried forward to Phase 1+:

* The legacy :class:`LLMRequest` shape collapses prompts to a
  single ``system`` + single user ``prompt`` string. If a
  :class:`StagePrompt` carries a non-empty ``assistant`` body
  (few-shot seeding), 0.5 logs an INFO event
  (``stage_router_assistant_role_dropped``) and proceeds with
  ``system`` + ``user`` only. Phase 1+ tasks needing assistant-role
  context will extend the provider interface.
* An empty ``response.content`` from a successful provider call
  (DeepSeek-observed quirk per vision §3 KD7) is treated as
  ``SEMANTIC`` and triggers immediate fallback. The ESC row for
  that attempt still records ``success=True`` because the call
  succeeded in transport terms; semantic interpretation is the
  router's policy decision.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.ladder_config import LadderConfig, LadderEntry
from course_supporter.llm.prompt_loader_md import StagePrompt, load_prompt
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.service_logging import _persist

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.llm.providers.base import LLMProvider

logger = structlog.get_logger()

# Exponential-backoff schedule clamp; matches KD16 "max 30s".
_MAX_BACKOFF_SECONDS = 30


@dataclass(frozen=True, slots=True)
class StageResult:
    """Final result returned by :meth:`StageRouter.execute_for_stage`.

    Attributes:
        content: The successful provider's response text.
        provider_used: Provider name from the winning ladder entry.
        model_used: Model id from the winning ladder entry.
        attempt_count: Total LLM calls including retries across all
            ladder entries -- useful for debugging fallback chains.
            Token / cost telemetry intentionally lives in ESC rows,
            not here.
    """

    content: str
    provider_used: str
    model_used: str
    attempt_count: int


class StageRouter:
    """KD16 router. Resolves a stage name to a fallback ladder.

    Caller responsibilities:

    * Set ``_current_job_id`` (and optionally ``_current_tenant_id``)
      ContextVars before invoking :meth:`execute_for_stage` so ESC
      rows persist -- :func:`service_logging._persist` skips writes
      with a WARNING when ``job_id`` is missing.
    * Catch :class:`LadderExhaustedError` for terminal failure.

    See module docstring for the fallback policy.
    """

    def __init__(
        self,
        ladder_config: LadderConfig,
        providers: dict[str, LLMProvider],
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        prompt_base_path: Path | None = None,
        max_retries_infrastructure: int = 3,
        max_retries_structural: int = 1,
    ) -> None:
        self._ladder_config = ladder_config
        self._providers = providers
        self._session_factory = session_factory
        self._prompt_base_path = prompt_base_path
        self._max_retries_infra = max_retries_infrastructure
        self._max_retries_structural = max_retries_structural

    async def execute_for_stage(
        self,
        stage_name: str,
        **render_context: Any,
    ) -> StageResult:
        """Execute the LLM call ladder for a named stage.

        Args:
            stage_name: Stage name registered in ``ladders_*.yaml``.
            **render_context: Variables for the prompt template's
                Jinja2 placeholders.

        Returns:
            :class:`StageResult` with the winning provider's content.

        Raises:
            KeyError: if ``stage_name`` is not registered.
            FileNotFoundError: if the stage's prompt file is missing.
            jinja2.UndefinedError: if a template variable is missing.
            LadderExhaustedError: if every ladder entry failed.
        """
        stage = self._ladder_config.get_stage(stage_name)
        prompt = load_prompt(
            stage.prompt_ref,
            base_path=self._prompt_base_path,
        ).render(**render_context)

        if prompt.assistant:
            logger.info(
                "stage_router_assistant_role_dropped",
                stage=stage_name,
                prompt_ref=stage.prompt_ref,
            )

        attempts: list[tuple[str, str, str]] = []
        total_attempt_count = 0

        for entry in stage.ladder:
            provider = self._providers.get(entry.provider)
            if provider is None:
                attempts.append(
                    (entry.provider, entry.model, "provider not configured")
                )
                continue
            if not provider.enabled:
                attempts.append((entry.provider, entry.model, "provider disabled"))
                continue

            request = self._build_request(prompt, entry, stage_name)
            response, used_count, reason = await self._attempt_entry(
                provider, entry, request, stage_name
            )
            total_attempt_count += used_count

            if response is not None:
                return StageResult(
                    content=response.content,
                    provider_used=entry.provider,
                    model_used=entry.model,
                    attempt_count=total_attempt_count,
                )

            attempts.append((entry.provider, entry.model, reason))

        raise LadderExhaustedError(stage_name, attempts)

    # ── Private helpers ─────────────────────────────────────────

    @staticmethod
    def _build_request(
        prompt: StagePrompt,
        entry: LadderEntry,
        stage_name: str,
    ) -> LLMRequest:
        """Map StagePrompt + LadderEntry to the legacy LLMRequest."""
        return LLMRequest(
            prompt=prompt.user or "",
            system_prompt=prompt.system,
            model=entry.model,
            action=stage_name,
            strategy="default",
        )

    async def _attempt_entry(
        self,
        provider: LLMProvider,
        entry: LadderEntry,
        request: LLMRequest,
        stage_name: str,
    ) -> tuple[LLMResponse | None, int, str]:
        """Walk one ladder entry: initial call + INFRASTRUCTURE retries.

        Returns ``(response, attempts_used, reason)``. On success
        ``response`` is non-None and ``reason`` is ``""``; otherwise
        ``response`` is ``None`` and ``reason`` describes why the
        entry was abandoned.
        """
        attempts_used = 0

        for attempt_idx in range(self._max_retries_infra + 1):
            attempts_used += 1
            try:
                response = await self._call_with_log(
                    provider, entry, request, stage_name
                )
            except StructuralRetryError as exc:
                (
                    retry_response,
                    retry_count,
                    retry_reason,
                ) = await self._structural_retry(
                    provider, entry, request, exc, stage_name
                )
                attempts_used += retry_count
                if retry_response is not None:
                    return retry_response, attempts_used, ""
                return None, attempts_used, retry_reason
            except Exception as exc:
                category = provider.classify_error(exc)
                if category is ErrorCategory.INFRASTRUCTURE:
                    if attempt_idx >= self._max_retries_infra:
                        return (
                            None,
                            attempts_used,
                            f"INFRASTRUCTURE: "
                            f"{self._max_retries_infra + 1} "
                            f"attempts exhausted - {exc}",
                        )
                    delay = min(2**attempt_idx, _MAX_BACKOFF_SECONDS)
                    await asyncio.sleep(delay)
                    continue
                if category is ErrorCategory.INPUT_OVERFLOW:
                    return None, attempts_used, f"INPUT_OVERFLOW: {exc}"
                # SEMANTIC, or STRUCTURAL classified by classifier without
                # being raised as StructuralRetryError -- nothing to retry
                # against in 0.5, so treat as immediate fallback.
                return None, attempts_used, f"{category.name}: {exc}"
            else:
                if not response.content:
                    return (
                        None,
                        attempts_used,
                        "SEMANTIC: empty response",
                    )
                return response, attempts_used, ""

        # Defensive: the loop always returns in the body above.
        return None, attempts_used, "INFRASTRUCTURE: retry loop exhausted"

    async def _structural_retry(
        self,
        provider: LLMProvider,
        entry: LadderEntry,
        original_request: LLMRequest,
        exc: StructuralRetryError,
        stage_name: str,
    ) -> tuple[LLMResponse | None, int, str]:
        """Single retry attempt with feedback appended to the user prompt."""
        retry_request = original_request.model_copy(
            update={
                "prompt": (
                    f"{original_request.prompt}\n\n"
                    f"[The previous response failed validation: "
                    f"{exc.feedback}. Please retry with a valid response.]"
                ),
            }
        )
        try:
            response = await self._call_with_log(
                provider, entry, retry_request, stage_name
            )
        except Exception as retry_exc:
            return None, 1, f"STRUCTURAL: retry exhausted - {retry_exc}"
        if not response.content:
            return (
                None,
                1,
                "SEMANTIC: empty response after structural retry",
            )
        return response, 1, ""

    async def _call_with_log(
        self,
        provider: LLMProvider,
        entry: LadderEntry,
        request: LLMRequest,
        stage_name: str,
    ) -> LLMResponse:
        """One LLM call. Persists ESC for the attempt in ``finally``."""
        start = time.perf_counter()
        response: LLMResponse | None = None
        error_message: str | None = None
        try:
            response = await provider.complete(request)
            return response
        except Exception as exc:
            error_message = str(exc)
            raise
        finally:
            if self._session_factory is not None:
                # ESC writes are telemetry; never let them mask a
                # provider exception or interrupt the router's
                # control flow. Mirrors the swallow doctrine inside
                # ``_persist`` itself (SQLAlchemyError / OSError) but
                # extends to any unexpected failure (logical bug,
                # async-context violation, etc.) so the original LLM
                # exception always wins propagation.
                fallback_latency_ms = int((time.perf_counter() - start) * 1000)
                try:
                    await _persist(
                        self._session_factory,
                        action=stage_name,
                        strategy="default",
                        provider=entry.provider,
                        model_id=entry.model,
                        unit_type="tokens",
                        unit_in=response.tokens_in if response else None,
                        unit_out=response.tokens_out if response else None,
                        latency_ms=(
                            response.latency_ms if response else fallback_latency_ms
                        ),
                        cost_usd=response.cost_usd if response else None,
                        success=response is not None,
                        error_message=error_message,
                    )
                except Exception:
                    logger.exception("stage_router_esc_persist_failed")
