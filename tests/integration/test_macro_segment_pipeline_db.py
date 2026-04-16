"""Integration test for the Stage 5 + 6 pipeline against real PostgreSQL.

Focus: round-trip persistence. The LLM call is mocked (via patched
``MacroTOCAgent``) so the test is hermetic, but every DB interaction —
``material_macro_sections``, ``material_segments``, CHECK constraints,
CASCADE delete, ``external_service_calls`` — runs against the live
schema.

Requires ``docker compose up -d`` (PostgreSQL).
Run with::

    uv run pytest tests/integration/test_macro_segment_pipeline_db.py \\
        --run-db -v
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.macro_toc import MacroTOCResult
from course_supporter.ingestion.macro_segment_pipeline import (
    run_text_macro_segment_pipeline,
)
from course_supporter.llm.schemas import LLMResponse
from course_supporter.models.macro_segment import (
    MacroSectionCandidate,
    MacroSectionsLLMOutput,
)
from course_supporter.storage.macro_section_repository import (
    MaterialMacroSectionRepository,
)
from course_supporter.storage.orm import (
    ExternalServiceCall,
    MacroSectionStatus,
    MaterialEntry,
    MaterialNode,
    Tenant,
)
from course_supporter.storage.segment_repository import MaterialSegmentRepository

pytestmark = pytest.mark.requires_db


SAMPLE_DOC = (
    "Introduction paragraph explaining the topic to the reader.\n\n"
    "## Functions in Python\n\n"
    "A function is a reusable block of code that performs a single task.\n\n"
    "Functions are defined with the ``def`` keyword and return values "
    "with ``return``.\n\n"
    "## Decorators\n\n"
    "Decorators are a powerful syntax for wrapping functions to modify "
    "their behaviour without changing their body.\n\n"
    "They are applied with the ``@decorator_name`` syntax directly above "
    "a function definition."
)


@pytest.fixture()
async def seeded_entry(db_session: AsyncSession) -> MaterialEntry:
    """Seed Tenant + root MaterialNode + text MaterialEntry with content."""
    tenant = Tenant(name=f"macro-seg-tenant-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()

    node = MaterialNode(
        tenant_id=tenant.id,
        title="Macro/Segment Course",
        order=0,
    )
    db_session.add(node)
    await db_session.flush()

    entry = MaterialEntry(
        materialnode_id=node.id,
        source_type="text",
        source_url="file:///sample.md",
        processed_content=SAMPLE_DOC,
        processed_hash="sha256-of-sample",
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


def _mock_llm_result() -> MacroTOCResult:
    """Deterministic LLM output matching SAMPLE_DOC exactly."""
    response = LLMResponse(
        content="",
        model_id="mock-model",
        provider="mock",
        tokens_in=500,
        tokens_out=120,
        cost_usd=0.001,
        latency_ms=50,
    )
    return MacroTOCResult(
        output=MacroSectionsLLMOutput(
            sections=[
                MacroSectionCandidate(
                    title="Introduction",
                    start_snippet=SAMPLE_DOC[:40],
                ),
                MacroSectionCandidate(
                    title="Functions",
                    start_snippet="## Functions in Python\n\nA function is a",
                ),
                MacroSectionCandidate(
                    title="Decorators",
                    start_snippet="## Decorators\n\nDecorators are a powerful",
                ),
            ]
        ),
        prompt_version="v1_macro_toc_text",
        response=response,
    )


class TestRunTextMacroSegmentPipeline:
    async def test_happy_path_populates_macro_and_segments(
        self,
        db_session: AsyncSession,
        seeded_entry: MaterialEntry,
    ) -> None:
        router = AsyncMock()
        with patch("course_supporter.agents.macro_toc.MacroTOCAgent") as agent_cls:
            agent_cls.return_value.run = AsyncMock(return_value=_mock_llm_result())

            await run_text_macro_segment_pipeline(
                session=db_session,
                entry=seeded_entry,
                router=router,
            )
            await db_session.commit()

        macro_repo = MaterialMacroSectionRepository(db_session)
        segment_repo = MaterialSegmentRepository(db_session)

        macros = await macro_repo.list_by_entry(seeded_entry.id)
        assert len(macros) == 3
        # Order preserved, contiguous coverage.
        assert [m.order for m in macros] == [0, 1, 2]
        assert macros[0].start_pos == 0
        assert macros[-1].end_pos == len(SAMPLE_DOC)
        for earlier, later in itertools.pairwise(macros):
            assert earlier.end_pos == later.start_pos

        # All statuses are ready — Stage 6 is synchronous for text.
        assert all(m.status == MacroSectionStatus.READY.value for m in macros)

        # Each macro has at least one segment; segments fully cover macro.
        for macro in macros:
            segments = await segment_repo.list_by_macro(macro.id)
            assert len(segments) >= 1
            assert segments[0].start_pos == macro.start_pos
            assert segments[-1].end_pos == macro.end_pos
            # Segments have NULL llm_call_id (deterministic, no LLM).
            assert all(s.llm_call_id is None for s in segments)
            # Content slices reconstruct the macro's text portion.
            reassembled = "".join(s.content for s in segments)
            assert reassembled == SAMPLE_DOC[macro.start_pos : macro.end_pos]

        # All macros reference the same LLM call row (one Stage-5 call).
        llm_call_ids = {m.llm_call_id for m in macros}
        assert len(llm_call_ids) == 1
        shared_call_id = llm_call_ids.pop()
        assert shared_call_id is not None

        # ExternalServiceCall row exists with correct action + cost.
        esc = await db_session.get(ExternalServiceCall, shared_call_id)
        assert esc is not None
        assert esc.action == "content_macro_toc"
        assert esc.success is True
        assert esc.cost_usd == pytest.approx(0.001)
        assert esc.prompt_ref == "v1_macro_toc_text"

    async def test_empty_processed_content_raises(
        self,
        db_session: AsyncSession,
        seeded_entry: MaterialEntry,
    ) -> None:
        seeded_entry.processed_content = None
        await db_session.commit()

        router = AsyncMock()
        with pytest.raises(ValueError, match="processed_content"):
            await run_text_macro_segment_pipeline(
                session=db_session,
                entry=seeded_entry,
                router=router,
            )

    async def test_cascade_delete_removes_macros_and_segments(
        self,
        db_session: AsyncSession,
        seeded_entry: MaterialEntry,
    ) -> None:
        """Deleting a MaterialEntry cascades to macros which cascade to segments."""
        router = AsyncMock()
        with patch("course_supporter.agents.macro_toc.MacroTOCAgent") as agent_cls:
            agent_cls.return_value.run = AsyncMock(return_value=_mock_llm_result())
            await run_text_macro_segment_pipeline(
                session=db_session,
                entry=seeded_entry,
                router=router,
            )
            await db_session.commit()

        macro_repo = MaterialMacroSectionRepository(db_session)
        assert len(await macro_repo.list_by_entry(seeded_entry.id)) == 3

        # Delete the entry — cascade should take everything with it.
        await db_session.delete(seeded_entry)
        await db_session.commit()

        remaining = await macro_repo.list_by_entry(seeded_entry.id)
        assert remaining == []


# Silence unused-import warnings for pytest-collected symbols.
_ = datetime, UTC
