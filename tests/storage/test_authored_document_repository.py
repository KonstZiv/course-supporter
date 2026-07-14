"""Real-DB tests for AuthoredDocumentRepository — Amendment 33-aligned.

Phase 1 commit (f) extends ``AuthoredDocumentRepository.create`` with
an optional ``course_root_id`` kwarg per vision §3 KD-delta. When the
caller supplies the value, the repository persists it directly (used
by ingestion pipeline that already has the root in scope). When the
caller omits it, the repository walks the parent chain via the
tenant-scoped variant of ``CourseNodeRepository.get_root_for``
(defense-in-depth per rule #12 — a malformed parent pointing at a
different tenant terminates the walk early and raises rather than
silently resolving to a foreign tenant's root).

Tests run against the real database (``requires_db`` marker) because
the resolution path is a recursive CTE walking ``parent_id`` over real
PostgreSQL — an in-memory SQLite surrogate would not honour the same
dialect or recursive CTE semantics.

Complement: ``tests/unit/test_authored_document_repository.py`` covers
Python flow + signature contracts via MagicMock (fast, no DB).
``tests/unit/test_authored_document.py`` covers the ORM model itself
(state derivation, FK config, field constraints) via MagicMock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Job, Tenant
from tests._helpers.course_node_factory import make_root_course_node


@pytest.fixture(scope="module")
async def kd_delta_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for KD-delta defensive-default tests.

    Bound to the test database from settings — the parent-walk CTE
    requires real PostgreSQL recursive-CTE semantics that an
    in-memory SQLite surrogate would not honour.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def kd_delta_session_factory(
    kd_delta_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        kd_delta_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, name_suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"kd-delta-{name_suffix}-{uuid.uuid4()}",
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_node(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    title: str,
) -> CourseNode:
    # Task 2.4.13B — root branch routes through the factory so the
    # CHECK-satisfying ``default_language`` lands here. Child branch
    # stays raw (task 2.4.13 рішення 1 — child language is dead data).
    if parent_id is None:
        node = make_root_course_node(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            title=title,
            order=0,
        )
    else:
        node = CourseNode(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            parent_id=parent_id,
            title=title,
            order=0,
        )
    session.add(node)
    await session.flush()
    return node


async def _cleanup_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_ids: list[uuid.UUID],
) -> None:
    """Tear down tenants + their authored_documents and course_nodes
    so each test leaves a clean slate. Order matters: documents first
    (FK cascade would handle it, but explicit deletion is robust under
    soft-delete-aware schemas)."""
    async with session_factory() as session:
        for tid in tenant_ids:
            await session.execute(
                delete(AuthoredDocument).where(
                    AuthoredDocument.course_root_id.in_(select_node_ids_for_tenant(tid))
                )
            )
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


def select_node_ids_for_tenant(tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Subquery for cleanup — yields node ids for a given tenant."""
    from sqlalchemy import select

    return select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)


@pytest.mark.requires_db
class TestCreateCourseRootIdDefensive:
    """``AuthoredDocumentRepository.create`` resolves ``course_root_id``
    via parent walk when not supplied (KD-delta default)."""

    async def test_create_with_explicit_same_tenant_course_root_id(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller-supplied same-tenant value is persisted unchanged.

        Verifies the trusted-caller pass-through path (e.g. ingestion
        pipeline that already has the root in scope from authenticated
        tenant context). Repository must NOT override the caller's
        value via the parent walk when explicit ``course_root_id`` is
        provided AND belongs to the same tenant as ``node_id``.

        Cross-tenant ``course_root_id`` is rejected — see
        :meth:`test_create_rejects_cross_tenant_course_root_id`.
        """
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "explicit-same")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            # Sentinel: a second root in the SAME tenant. Repository
            # must honour the caller's choice (skip parent walk) but
            # still reject cross-tenant attempts.
            extra_root = await _make_node(
                session,
                tenant_id=tenant.id,
                parent_id=None,
                title="extra-root-same-tenant",
            )
            await session.commit()
            tenant_id = tenant.id
            child_id = child.id
            extra_root_id = extra_root.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=child_id,
                source_type="text",
                source_url="https://example.com/explicit-same",
                course_root_id=extra_root_id,
            )
            await session.commit()
            # Caller value honoured: extra_root_id NOT child's parent
            # walk-derived root; repository did not recompute.
            assert doc.course_root_id == extra_root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_rejects_cross_tenant_course_root_id(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cross-tenant ``course_root_id`` raises ValueError (rule #12).

        Per AI ReviewBot Critical [1] (cross-check session) +
        vision-side ratify: prior to hotfix-13, the repository accepted
        caller-supplied cross-tenant ``course_root_id`` without
        validation, creating a vector for cross-tenant data leak via
        KD-delta denormalization scope-filtering. This negative test
        guards the defense-in-depth check matching rule #12 pattern
        (analogous to the parent_id walk in
        :meth:`_resolve_course_root_id`).
        """
        async with kd_delta_session_factory() as session:
            tenant_a = await _make_tenant(session, "victim-a")
            tenant_b = await _make_tenant(session, "attacker-b")
            root_a = await _make_node(
                session, tenant_id=tenant_a.id, parent_id=None, title="root-a"
            )
            child_a = await _make_node(
                session, tenant_id=tenant_a.id, parent_id=root_a.id, title="child-a"
            )
            # Foreign-tenant root that an attacker might try to inject
            # via course_root_id parameter.
            root_b = await _make_node(
                session, tenant_id=tenant_b.id, parent_id=None, title="root-b"
            )
            await session.commit()
            tenant_ids = [tenant_a.id, tenant_b.id]
            child_a_id = child_a.id
            root_b_id = root_b.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError, match=r"cross-tenant violation"):
                await repo.create(
                    node_id=child_a_id,
                    source_type="text",
                    source_url="https://example.com/cross-tenant-attempt",
                    course_root_id=root_b_id,
                )

        await _cleanup_tenant(kd_delta_session_factory, tenant_ids)

    async def test_create_computes_root_when_not_provided(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller omits ``course_root_id`` → repository walks parent
        chain, persists the actual root id."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "compute")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            mid = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="mid"
            )
            leaf = await _make_node(
                session, tenant_id=tenant.id, parent_id=mid.id, title="leaf"
            )
            await session.commit()
            root_id, leaf_id, tenant_id = root.id, leaf.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=leaf_id,
                source_type="text",
                source_url="https://example.com/computed",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_handles_root_node_input(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``node_id`` is itself the root (parent_id IS NULL) →
        ``course_root_id`` resolves to ``node_id``."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "rootinput")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root-only"
            )
            await session.commit()
            root_id, tenant_id = root.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="text",
                source_url="https://example.com/at-root",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_handles_deep_nesting(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """4-level tree (root → A → B → C); ``course_root_id`` from
        ``C`` walks all the way up."""
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "deep")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            a = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="A"
            )
            b = await _make_node(
                session, tenant_id=tenant.id, parent_id=a.id, title="B"
            )
            c = await _make_node(
                session, tenant_id=tenant.id, parent_id=b.id, title="C"
            )
            await session.commit()
            root_id, c_id, tenant_id = root.id, c.id, tenant.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=c_id,
                source_type="text",
                source_url="https://example.com/deep",
            )
            await session.commit()
            assert doc.course_root_id == root_id

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_tenant_isolation_cross_tenant_parent_raises(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Defense-in-depth: when ``parent_id`` chain crosses tenants
        (data corruption scenario), the tenant-scoped walk terminates
        early and ``create`` raises rather than silently assigning a
        foreign tenant's root id (rule #12).
        """
        async with kd_delta_session_factory() as session:
            tenant_a = await _make_tenant(session, "tenant-a")
            tenant_b = await _make_tenant(session, "tenant-b")
            root_a = await _make_node(
                session, tenant_id=tenant_a.id, parent_id=None, title="root-a"
            )
            # Corrupt scenario: a node in tenant B points at tenant A's
            # root. Real-world this would be a bug or replication
            # artefact; the test asserts the defensive default refuses
            # to silently resolve.
            corrupted = await _make_node(
                session,
                tenant_id=tenant_b.id,
                parent_id=root_a.id,
                title="cross-tenant-child",
            )
            await session.commit()
            corrupted_id = corrupted.id
            tenant_ids = [tenant_a.id, tenant_b.id]

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError, match=r"Cannot resolve course_root_id"):
                await repo.create(
                    node_id=corrupted_id,
                    source_type="text",
                    source_url="https://example.com/cross-tenant",
                )

        await _cleanup_tenant(kd_delta_session_factory, tenant_ids)

    async def test_create_raises_when_node_id_does_not_exist(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Sanity guard: a non-existent ``node_id`` produces a clear
        error rather than a confusing FK constraint violation deeper
        in the call stack.
        """
        nonexistent = uuid.uuid4()
        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError, match=r"CourseNode not found"):
                await repo.create(
                    node_id=nonexistent,
                    source_type="text",
                    source_url="https://example.com/missing",
                )


@pytest.mark.requires_db
class TestCompleteProcessingStateTransition:
    """``complete_processing`` flips the document from PENDING to READY.

    Hotfix-9 (regression D re-classified): the prior body was a no-op
    stub left over from Phase 1 rename pass — caller expected it to
    drive the state transition but it returned ``None`` silently,
    leaving every successfully-ingested document stuck in PENDING. The
    new body clears the pending receipt (``job_id`` + ``pending_since``
    + defensive ``error_message``) and stamps ``processed_at``; the
    ``state`` derivation property reads ``job_id IS NULL`` as READY.

    Vision §1.2 explicitly removes ``processed_content`` /
    ``outline_content`` / ``processed_hash`` columns from the authored
    layer (Phase 2.x KD2 will populate DocumentSummary + DocumentSegment
    instead) — the method signature drops those parameters in this
    hotfix. State transition is the entire current-Phase responsibility.

    Tests run against the real database (``requires_db`` marker) per
    Amendment 33: state derivation depends on actual row state across
    a session boundary, and the existing ``set_pending`` / ``fail_processing``
    counterparts already lock to the same fixture pattern.
    """

    async def test_clears_pending_receipt_and_stamps_processed_at(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Canonical pending → ready transition:

        * ``job_id`` cleared (state.PENDING pivot)
        * ``pending_since`` cleared (receipt blanked)
        * ``error_message`` cleared (defensive; survives a prior failed try)
        * ``processed_at`` set to a non-NULL timestamp
        * ``state`` property returns ``MaterialState.READY``
        """
        from course_supporter.storage.orm import MaterialState

        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "complete-processing")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            job = Job(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type="document_processing",
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()
            tenant_id, root_id, job_id = tenant.id, root.id, job.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="text",
                source_url="https://example.com/transition",
            )
            # Mimic an in-flight ingestion: receipt set, optional prior
            # error from a previous failed attempt.
            doc.job_id = job_id
            doc.pending_since = datetime.now(UTC)
            doc.error_message = "previous-attempt-error"
            await session.commit()
            doc_id = doc.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            updated = await repo.complete_processing(doc_id)
            await session.commit()

            assert updated.id == doc_id
            assert updated.job_id is None
            assert updated.pending_since is None
            assert updated.error_message is None
            assert updated.processed_at is not None
            assert updated.state == MaterialState.READY

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_now_override_is_honoured(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The optional ``now`` parameter pins ``processed_at`` for
        deterministic testing — mirrors :meth:`set_pending` and
        :meth:`fail_processing` patterns.
        """
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "complete-now-override")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            job = Job(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                course_node_id=root.id,
                job_type="document_processing",
                status="active",
            )
            session.add(job)
            await session.flush()
            await session.commit()
            tenant_id, root_id, job_id = tenant.id, root.id, job.id

        fixed = datetime(2026, 5, 5, 17, 0, 0, tzinfo=UTC)

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="text",
                source_url="https://example.com/now-override",
            )
            doc.job_id = job_id
            doc.pending_since = datetime(2026, 5, 5, 16, 0, 0, tzinfo=UTC)
            await session.commit()
            doc_id = doc.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            updated = await repo.complete_processing(doc_id, now=fixed)
            await session.commit()

            assert updated.processed_at == fixed

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_raises_when_entry_does_not_exist(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Symmetric guard with the rest of the repository — a missing
        entry yields a clean ValueError rather than crashing on a NULL
        attribute access mid-transition.
        """
        nonexistent = uuid.uuid4()
        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            with pytest.raises(ValueError):
                await repo.complete_processing(nonexistent)


@pytest.mark.requires_db
class TestCreateRawHash:
    """``AuthoredDocumentRepository.create`` persists optional raw_hash
    kwarg (Phase 2.1 C8 / KD-2.1-E / closes DD-1.1-1).

    Strategy A: the upload entry points compute SHA-256 over the bytes
    they already hold (Stage 1 requires full-buffer materialisation)
    and forward the hex digest into ``create()``. Pre-C8 the column
    was nullable and never populated; post-C8 every file-backed entry
    carries a hash, while URL-only entries legitimately stay ``None``
    until the ingestion worker fetches the bytes (out-of-scope for C8).
    """

    async def test_create_with_raw_hash_persists_value(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "raw-hash-explicit")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            await session.commit()
            tenant_id = tenant.id
            root_id = root.id

        # 64-char lowercase hex digest (mirror SHA-256 hex shape).
        digest = "a" * 64

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="text",
                source_url="https://example.com/raw-hash-explicit",
                raw_hash=digest,
            )
            await session.commit()
            assert doc.raw_hash == digest

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])

    async def test_create_without_raw_hash_defaults_to_none(
        self,
        kd_delta_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Backwards-compat: callers that omit raw_hash get None.

        Existing pre-C8 call sites (URL-only paths, legacy fixtures)
        keep working without modification; raw_hash is opt-in via
        keyword argument.
        """
        async with kd_delta_session_factory() as session:
            tenant = await _make_tenant(session, "raw-hash-omitted")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            await session.commit()
            tenant_id = tenant.id
            root_id = root.id

        async with kd_delta_session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            doc = await repo.create(
                node_id=root_id,
                source_type="web",
                source_url="https://example.com/raw-hash-omitted",
            )
            await session.commit()
            assert doc.raw_hash is None

        await _cleanup_tenant(kd_delta_session_factory, [tenant_id])
