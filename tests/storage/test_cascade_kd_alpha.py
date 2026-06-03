"""KD-alpha invariants: ORM-level ``delete-orphan`` strip + decoupling.

Phase 1 commit (d) replaces ``cascade="all, delete-orphan"`` with
``cascade="save-update, merge"`` on the six active soft-deletable
relationship pairs (Amendment 1) so an accidental ORM-level
``session.delete(parent)`` cannot bypass the soft-delete cascade,
``s3_cleanup`` enqueue, or KD-β scrubbing — soft-deletion flows
exclusively through ``CascadeDeleteService`` per vision §3 KD3.

Two invariants are locked here:

1. **Survival** — after the strip, the SQLAlchemy ``relationship.cascade``
   set on each of the six active pairs no longer contains ``"delete"``
   or ``"delete-orphan"``. ``"save-update"`` and ``"merge"`` are
   preserved per PHASE.md §2.7 KD-alpha (so adding a parent to the session
   still implicitly adds in-memory children — autoflush convenience —
   without propagating the deletion verb).

2. **Decoupling** — ``CourseNodeRepository.get_subtree`` uses
   ``set_committed_value`` to wire ``children`` / ``parent`` after the
   single CTE-driven load. That bypasses the SQLAlchemy attribute event
   system, so the freshly assembled tree carries no "dirty" markers and
   the next ``flush()`` on the session emits **no** UPDATE or DELETE
   statements that would trip the soft-delete protection trigger or
   silently rewrite columns.

The Phase-5-bound relationships (``CourseNode.snapshots``,
``CourseNode.editables``, ``StructureNode.children``,
``StructureNodeEditable.children``, ``StructureSnapshot.structure_nodes``)
were removed in C9.4 along with the StructureSnapshot family; the
``TestPhase5BoundRelationshipsPreserved`` block that locked their
preservation no longer applies and was dropped with the ORM classes.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSummary,
    Student,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

# ── Invariant 1: Survival (mapper inspection — no DB) ─────────────


class TestKDAlphaStrippedRelationships:
    """Six active soft-deletable relationships no longer carry
    ``delete`` / ``delete-orphan`` cascade verbs after Phase 1 commit (d).
    """

    def test_tenant_api_keys_strip(self) -> None:
        cascade = Tenant.api_keys.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade

    def test_course_node_children_strip(self) -> None:
        # Self-referential tree — strip is critical because an accidental
        # ``session.delete(root)`` would otherwise wipe the entire course.
        cascade = CourseNode.children.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade

    def test_course_node_documents_strip(self) -> None:
        cascade = CourseNode.documents.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade

    def test_authored_document_summary_strip(self) -> None:
        # 1:1 reshape (uselist=False) since commit (b); strip applies
        # equally to the 1:1 form.
        cascade = AuthoredDocument.summary.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade

    def test_document_summary_segments_strip(self) -> None:
        cascade = DocumentSummary.segments.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade

    def test_student_submissions_strip(self) -> None:
        cascade = Student.submissions.property.cascade
        assert "delete" not in cascade
        assert "delete-orphan" not in cascade
        assert "save-update" in cascade
        assert "merge" in cascade


# ── Invariant 2: Decoupling (real DB, observational) ──────────────


@pytest.fixture(scope="module")
async def kd_alpha_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for KD-alpha decoupling observation.

    Bound to the test database from settings — KD-alpha decoupling is a
    real-session invariant (we observe the *actual* SQL emitted by
    ``set_committed_value``-using code paths), so an in-memory SQLite
    surrogate would not exercise the same dialect or autoflush mode.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def kd_alpha_session_factory(
    kd_alpha_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        kd_alpha_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.mark.requires_db
class TestGetSubtreeDecoupling:
    """``CourseNodeRepository.get_subtree`` must not emit any DELETE or
    UPDATE statements during tree assembly — ``set_committed_value``
    bypasses dirty-tracking so loaded children/parent links are not
    seen as modifications by the next ``flush()``.
    """

    async def test_get_subtree_emits_no_writes(
        self,
        kd_alpha_engine: AsyncEngine,
        kd_alpha_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Set up: tenant + 3-node tree (root → child → grandchild).
        async with kd_alpha_session_factory() as session:
            tenant = Tenant(
                id=uuid.uuid4(),
                name=f"kd-alpha-decoupling-{uuid.uuid4()}",
                is_active=True,
            )
            session.add(tenant)
            await session.flush()

            root = make_root_course_node(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                title="root",
                order=0,
            )
            child = CourseNode(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                parent_id=root.id,
                title="child",
                order=0,
            )
            grandchild = CourseNode(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                parent_id=child.id,
                title="grandchild",
                order=0,
            )
            session.add_all([root, child, grandchild])
            await session.commit()
            root_id = root.id

        # Observe: capture SQL emitted during get_subtree → flush.
        emitted: list[str] = []

        @event.listens_for(kd_alpha_engine.sync_engine, "before_cursor_execute")
        def capture(  # type: ignore[no-untyped-def]
            _conn, _cursor, statement: str, _params, _context, _executemany
        ) -> None:
            emitted.append(statement.strip())

        try:
            async with kd_alpha_session_factory() as session:
                repo = CourseNodeRepository(session)
                tree = await repo.get_subtree(root_id)
                # Force any pending unit-of-work to drain.
                await session.flush()
                assert tree, "get_subtree returned an empty list"
        finally:
            event.remove(kd_alpha_engine.sync_engine, "before_cursor_execute", capture)

        # Assertion: only SELECTs (and dialect-internal SAVEPOINTs); no
        # DELETE or UPDATE on the course_nodes table. Word-boundary regex
        # catches statements at start, after CTE prefix (``WITH ... DELETE
        # FROM ...``), and after multi-statement ``;`` boundaries — naive
        # space-padding (`` DELETE ``) misses the start-of-statement form.
        delete_or_update = re.compile(
            r"\b(DELETE\s+FROM|UPDATE)\s",
            re.IGNORECASE,
        )
        bad = [
            stmt
            for stmt in emitted
            if delete_or_update.search(stmt) and "course_nodes" in stmt.lower()
        ]
        assert bad == [], f"get_subtree emitted unexpected mutating SQL: {bad}"

        # Cleanup: tear down the tenant subtree.
        async with kd_alpha_session_factory() as session:
            await session.execute(
                delete(CourseNode).where(CourseNode.tenant_id == tenant.id)
            )
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()
