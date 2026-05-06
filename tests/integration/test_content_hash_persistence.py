"""Integration: ContentHashService against real PostgreSQL (vision §3 KD9).

Drives the actual SQL queries (children SELECTs, parent navigation),
the soft-delete trigger interaction, and the per-entity formula
dispatch end-to-end. Algebra and walker logic are covered by unit
tests; this suite verifies the persistence path.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    Tenant,
)

pytestmark = pytest.mark.requires_db


# ── Fixture helpers ──────────────────────────────────────────────


async def _make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"hash-test-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    parent_id: uuid.UUID | None = None,
    title: str = "node",
) -> CourseNode:
    node = CourseNode(
        tenant_id=tenant_id,
        parent_id=parent_id,
        title=title,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_entry(
    session: AsyncSession,
    node_id: uuid.UUID,
    *,
    raw_hash: str | None = None,
    source_url: str | None = None,
) -> AuthoredDocument:
    entry = AuthoredDocument(
        course_node_id=node_id,
        source_type="text",
        source_url=source_url or f"https://example.com/{uuid.uuid4().hex[:8]}",
        raw_hash=raw_hash,
    )
    session.add(entry)
    await session.flush()
    return entry


async def _make_section(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    order: int = 0,
    title: str = "section",
) -> DocumentSummary:
    section = DocumentSummary(
        authored_document_id=entry_id,
        order=order,
        title=title,
        start_pos=0,
        end_pos=100,
        status="ready",
    )
    session.add(section)
    await session.flush()
    return section


async def _make_segment(
    session: AsyncSession,
    section_id: uuid.UUID,
    *,
    order: int = 0,
    content: str = "default content",
    start_pos: int = 0,
    end_pos: int = 50,
) -> DocumentSegment:
    segment = DocumentSegment(
        document_summary_id=section_id,
        order=order,
        start_pos=start_pos,
        end_pos=end_pos,
        content=content,
    )
    session.add(segment)
    await session.flush()
    return segment


# ── invalidate_up writes hashes on real DB rows ──────────────────


class TestInvalidateUpPersistsHashes:
    async def test_walks_full_chain_from_segment_to_root(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_node(db_session, tenant.id, title="root")
        entry = await _make_entry(db_session, node.id, raw_hash="a" * 64)
        section = await _make_section(db_session, entry.id)
        segment = await _make_segment(db_session, section.id, content="hello")

        # All hashes are NULL initially (no backfill in 0.2).
        assert segment.content_hash is None
        assert section.content_hash is None
        assert entry.content_hash is None
        assert node.content_hash is None

        await ContentHashService(db_session).invalidate_up(segment)

        # All four levels populated.
        await db_session.refresh(segment)
        await db_session.refresh(section)
        await db_session.refresh(entry)
        await db_session.refresh(node)
        assert segment.content_hash is not None
        assert section.content_hash is not None
        assert entry.content_hash is not None
        assert node.content_hash is not None
        # All 64-char lowercase hex.
        for h in (
            segment.content_hash,
            section.content_hash,
            entry.content_hash,
            node.content_hash,
        ):
            assert len(h) == 64

    async def test_segment_content_change_propagates_up(
        self, db_session: AsyncSession
    ) -> None:
        """Re-hashing a segment ripples to MacroSection → Entry → Node."""
        tenant = await _make_tenant(db_session)
        node = await _make_node(db_session, tenant.id)
        entry = await _make_entry(db_session, node.id, raw_hash="b" * 64)
        section = await _make_section(db_session, entry.id)
        segment = await _make_segment(db_session, section.id, content="initial")

        svc = ContentHashService(db_session)
        await svc.invalidate_up(segment)

        initial = (
            segment.content_hash,
            section.content_hash,
            entry.content_hash,
            node.content_hash,
        )

        # Mutate segment.content (in formula) and re-invalidate.
        segment.content = "changed"
        await svc.invalidate_up(segment)

        # All four hashes changed.
        assert segment.content_hash != initial[0]
        assert section.content_hash != initial[1]
        assert entry.content_hash != initial[2]
        assert node.content_hash != initial[3]


# ── short-circuit: metadata-only changes do not propagate ────────


class TestInvalidateUpShortCircuit:
    async def test_metadata_only_change_does_not_propagate(
        self, db_session: AsyncSession
    ) -> None:
        """Update segment.order (metadata, NOT in segment formula) — no cascade.

        The segment hash formula in 0.2 only reads ``content``; ordering
        and positions join the formula in Phase 1.4 (KD9 line 552). So
        an ``order`` change should leave segment.content_hash unchanged
        → walk short-circuits → parent and grandparent untouched.
        """
        tenant = await _make_tenant(db_session)
        node = await _make_node(db_session, tenant.id)
        entry = await _make_entry(db_session, node.id, raw_hash="c" * 64)
        section = await _make_section(db_session, entry.id)
        seg_a = await _make_segment(db_session, section.id, order=0, content="a")
        seg_b = await _make_segment(db_session, section.id, order=1, content="b")

        svc = ContentHashService(db_session)
        await svc.invalidate_up(seg_a)
        await svc.invalidate_up(seg_b)

        before = {
            "seg_a": seg_a.content_hash,
            "seg_b": seg_b.content_hash,
            "section": section.content_hash,
            "entry": entry.content_hash,
            "node": node.content_hash,
        }
        assert all(v is not None for v in before.values())

        # Metadata-only change on seg_a (``order`` is metadata in 0.2,
        # not in the segment hash formula).
        seg_a.order = 99

        await svc.invalidate_up(seg_a)

        # seg_a recomputed but identical (formula uses content only).
        assert seg_a.content_hash == before["seg_a"]
        # Sibling untouched (we never visited it).
        assert seg_b.content_hash == before["seg_b"]
        # Parent / grand-parent / great-grand-parent untouched (short-circuit).
        assert section.content_hash == before["section"]
        assert entry.content_hash == before["entry"]
        assert node.content_hash == before["node"]
