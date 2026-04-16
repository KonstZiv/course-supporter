"""Orchestrator for Stage 5 + Stage 6 (text / web sources).

End-to-end flow:

1. Load ``MaterialEntry.processed_content``.
2. Run Stage-5 LLM via :class:`MacroTOCAgent` → raw snippet candidates.
3. Resolve snippets to absolute char offsets via
   :func:`resolve_macro_sections` (full-coverage invariants).
4. Persist macro section rows with ``status=ready``.
5. For each macro: paragraph-split the content slice (Stage 6,
   deterministic — no LLM) and persist segments.
6. Log a single ``ExternalServiceCall`` row for the Stage-5 call.

The function is source-type agnostic from the caller's point of view —
it operates on ``processed_content`` as Markdown text. The ARQ task
dispatches here for ``text`` and ``web`` ``MaterialEntry``s.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from course_supporter.ingestion.macro_segment_helpers import (
    resolve_macro_sections,
    split_into_segments,
)
from course_supporter.storage.macro_section_repository import (
    MaterialMacroSectionRepository,
)
from course_supporter.storage.orm import ExternalServiceCall, MacroSectionStatus
from course_supporter.storage.segment_repository import MaterialSegmentRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import MaterialEntry

logger = structlog.get_logger()


async def run_text_macro_segment_pipeline(
    *,
    session: AsyncSession,
    entry: MaterialEntry,
    router: ModelRouter,
) -> None:
    """Run Stage 5 + 6 for a text / web material.

    The caller owns the session / transaction. On success this function
    flushes all new rows; callers are expected to ``session.commit()``.
    On failure the caller should roll back and mark the upstream Job as
    failed; no partial state is committed here.

    Args:
        session: Active SQLAlchemy session.
        entry: MaterialEntry that has already been successfully processed
            by Stage 1 (``entry.processed_content`` is populated).
        router: ModelRouter for the Stage-5 LLM call.

    Raises:
        ValueError: If ``entry.processed_content`` is empty / missing.
        SnippetResolutionError: If any snippet cannot be located in
            ``processed_content`` (see helpers for semantics).
        AllModelsFailedError: If every model in the Stage-5 chain fails.
    """
    from course_supporter.agents.macro_toc import MacroTOCAgent

    content = entry.processed_content
    if not content:
        msg = (
            f"MaterialEntry {entry.id} has no processed_content; "
            "Stage 5 / 6 require Stage 1 to have completed."
        )
        raise ValueError(msg)

    log = logger.bind(
        material_entry_id=str(entry.id),
        source_type=entry.source_type,
        content_chars=len(content),
    )
    log.info("macro_segment_pipeline_start")

    # ── Stage 5: LLM macro-TOC ──────────────────────────────────────
    agent = MacroTOCAgent(router)
    agent_result = await agent.run(processed_content=content)
    response = agent_result.response

    # Persist cost-tracking row up front so we keep the audit trail
    # even if downstream steps fail.
    esc = ExternalServiceCall(
        action="content_macro_toc",
        provider=response.provider,
        model_id=response.model_id,
        prompt_ref=agent_result.prompt_version,
        unit_type="tokens",
        unit_in=response.tokens_in,
        unit_out=response.tokens_out,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
        success=True,
    )
    session.add(esc)
    await session.flush()
    llm_call_id: uuid.UUID = esc.id

    # ── Resolve snippets → absolute offsets ─────────────────────────
    macro_drafts = resolve_macro_sections(
        candidates=agent_result.output.sections,
        processed_content=content,
    )

    # ── Persist macro section rows ──────────────────────────────────
    macro_repo = MaterialMacroSectionRepository(session)
    macro_rows = await macro_repo.batch_create(
        material_entry_id=entry.id,
        drafts=macro_drafts,
        llm_call_id=llm_call_id,
        status=MacroSectionStatus.READY,
    )

    # ── Stage 6: deterministic paragraph split per macro ────────────
    segment_repo = MaterialSegmentRepository(session)
    total_segments = 0
    for row in macro_rows:
        macro_slice = content[row.start_pos : row.end_pos]
        seg_drafts = split_into_segments(
            macro_content=macro_slice,
            macro_start_pos=row.start_pos,
        )
        await segment_repo.batch_create(
            macro_section_id=row.id,
            drafts=seg_drafts,
            llm_call_id=None,  # text / web: deterministic, no LLM
        )
        total_segments += len(seg_drafts)

    log.info(
        "macro_segment_pipeline_done",
        macro_sections=len(macro_rows),
        segments=total_segments,
        prompt_version=agent_result.prompt_version,
        model=response.model_id,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        cost_usd=response.cost_usd,
    )
