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

from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.content_hash import (
    ContentHashService,
    compute_content_hash,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
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
    course_root_id: uuid.UUID | None = None,
) -> AuthoredDocument:
    entry = AuthoredDocument(
        course_node_id=node_id,
        course_root_id=course_root_id if course_root_id is not None else node_id,
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
    title: str = "section",
) -> DocumentSummary:
    # Inherit course_root_id from parent AuthoredDocument per KD-delta
    # (schema comment at orm.py: ``Inherited from
    # authored_documents.course_root_id at INSERT``).
    entry = await session.get(AuthoredDocument, entry_id)
    if entry is None:
        msg = f"AuthoredDocument {entry_id} not found"
        raise ValueError(msg)
    section = DocumentSummary(
        authored_document_id=entry_id,
        course_root_id=entry.course_root_id,
        title=title,
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
    # Inherit course_root_id from parent DocumentSummary per KD-delta.
    section = await session.get(DocumentSummary, section_id)
    if section is None:
        msg = f"DocumentSummary {section_id} not found"
        raise ValueError(msg)
    segment = DocumentSegment(
        document_summary_id=section_id,
        course_root_id=section.course_root_id,
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


# ── D4: branch isolation at the CourseNode level (Phase 1.1) ─────


class TestInvalidateUpBranchIsolation:
    """Branch-independence at the CourseNode level (D4 — Phase 1.1).

    Walker direction guarantees: ``_walk_up`` only traverses the parent
    chain, never siblings. Modifying content under one branch must NOT
    propagate the hash change to sibling branches — only the common
    ancestor is shared, and its hash recomputes from current children
    leaving the sibling's own hash literally unchanged.

    Migrated from ``tests/unit/test_fingerprint.py::TestBranchIndependence``
    (sub-test ``test_invalidation_preserves_other_branch``); ratified at
    CourseNode level (D4 variant "ii") rather than DocumentSegment level
    for stronger coverage of the canonical tree structure.
    """

    async def test_modify_one_branch_leaves_sibling_branch_untouched(
        self, db_session: AsyncSession
    ) -> None:
        """Modifying ``docA`` propagates up branchA only; branchB intact."""
        # Tree shape:
        #          root
        #         /    \
        #     branchA  branchB
        #       |        |
        #     leafA    leafB
        #       |        |
        #     docA      docB
        #
        # Documents created via the repository (not the ``_make_entry``
        # helper) so the canonical KD-delta ``course_root_id`` resolution
        # runs and the etap-1.1.4 ``invalidate_up`` post-flush wiring
        # populates the initial hash chain in one go.
        tenant = await _make_tenant(db_session)
        doc_repo = AuthoredDocumentRepository(db_session)

        root = await _make_node(db_session, tenant.id, title="root")
        branch_a = await _make_node(
            db_session, tenant.id, parent_id=root.id, title="branchA"
        )
        branch_b = await _make_node(
            db_session, tenant.id, parent_id=root.id, title="branchB"
        )
        leaf_a = await _make_node(
            db_session, tenant.id, parent_id=branch_a.id, title="leafA"
        )
        leaf_b = await _make_node(
            db_session, tenant.id, parent_id=branch_b.id, title="leafB"
        )
        doc_a = await doc_repo.create(
            node_id=leaf_a.id,
            source_type="text",
            source_url="https://example.com/branch-isolation-a",
        )
        doc_b = await doc_repo.create(
            node_id=leaf_b.id,
            source_type="text",
            source_url="https://example.com/branch-isolation-b",
        )

        for obj in (doc_a, doc_b, leaf_a, leaf_b, branch_a, branch_b, root):
            await db_session.refresh(obj)

        before = {
            "doc_a": doc_a.content_hash,
            "doc_b": doc_b.content_hash,
            "leaf_a": leaf_a.content_hash,
            "leaf_b": leaf_b.content_hash,
            "branch_a": branch_a.content_hash,
            "branch_b": branch_b.content_hash,
            "root": root.content_hash,
        }
        assert all(v is not None for v in before.values())

        # Mutate raw_hash on docA (formula field for AuthoredDocument)
        # then drive the canonical walker explicitly. After the walk,
        # docA's hash reflects the new raw_hash bytes and the change
        # propagates to leafA → branchA → root.
        svc = ContentHashService(db_session)
        doc_a.raw_hash = "c" * 64
        await db_session.flush()
        await svc.invalidate_up(doc_a)
        for obj in (doc_a, leaf_a, branch_a, root, leaf_b, branch_b, doc_b):
            await db_session.refresh(obj)

        # Branch A chain: hash flows up to root.
        assert doc_a.content_hash != before["doc_a"]
        assert leaf_a.content_hash != before["leaf_a"]
        assert branch_a.content_hash != before["branch_a"]
        assert root.content_hash != before["root"]

        # Branch B chain: KEY assertions — sibling branch UNTOUCHED.
        assert doc_b.content_hash == before["doc_b"]
        assert leaf_b.content_hash == before["leaf_b"]
        assert branch_b.content_hash == before["branch_b"]


# ── etap 1.1.4 acceptance: create()-time materialisation ─────────


class TestCreateMaterializesContentHash:
    """``create()`` materialises ``content_hash`` at INSERT time per KD9.

    Etap 1.1.4 fix for the KD9 NULL-on-INSERT regression (vision §3 KD9
    line 580 observation: course_nodes.content_hash arrives NULL at
    INSERT time and is populated only after the first PATCH). Both
    ``CourseNodeRepository.create`` and ``AuthoredDocumentRepository.create``
    invoke ``ContentHashService.invalidate_up`` post-flush so the new
    entity carries a canonical non-NULL hash without waiting for any
    downstream cascade-delete event.
    """

    async def test_course_node_create_materializes_content_hash(
        self, db_session: AsyncSession
    ) -> None:
        """Brand-new CourseNode has empty-Merkle hash, not NULL."""
        tenant = await _make_tenant(db_session)
        repo = CourseNodeRepository(db_session)
        node = await repo.create(tenant_id=tenant.id, title="freshly-created")
        await db_session.refresh(node)

        # New empty node = no AuthoredDocs + no child CourseNodes →
        # canonical formula yields ``compute_content_hash(b"", [])``.
        # This is the well-known empty SHA-256 baseline for the Merkle
        # algebra; importantly, it is not NULL — anti-regression on the
        # KD9 line 580 observation.
        expected = compute_content_hash(b"", [])
        assert node.content_hash is not None
        assert node.content_hash == expected

    async def test_authored_document_create_materializes_content_hash(
        self, db_session: AsyncSession
    ) -> None:
        """Brand-new AuthoredDocument has materialised hash + parent hash recomputed."""
        tenant = await _make_tenant(db_session)
        node_repo = CourseNodeRepository(db_session)
        node = await node_repo.create(tenant_id=tenant.id, title="parent")
        await db_session.refresh(node)
        node_hash_before_doc = node.content_hash

        doc_repo = AuthoredDocumentRepository(db_session)
        doc = await doc_repo.create(
            node_id=node.id,
            source_type="text",
            source_url="https://example.com/phase-1-1-c1-acceptance",
        )
        await db_session.refresh(doc)
        await db_session.refresh(node)

        # New doc: no DocumentSummaries yet, raw_hash NULL (Phase 2.x
        # ingestion populates it). Formula falls back to ``b""`` for
        # raw_hash; with no summary children this yields the empty
        # Merkle baseline. Non-NULL is the regression-relevant fact.
        empty_hash = compute_content_hash(b"", [])
        assert doc.content_hash is not None
        assert doc.content_hash == empty_hash

        # Parent node hash recomputed: now contains one AuthoredDocument
        # child (whose content_hash equals empty_hash). The Merkle
        # aggregation differs from the pre-doc empty-baseline because
        # the children list is non-empty.
        assert node.content_hash is not None
        assert node.content_hash != node_hash_before_doc
