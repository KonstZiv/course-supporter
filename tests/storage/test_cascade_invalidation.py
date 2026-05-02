"""Tests for Gap 3 (``on_invalidate_hashes`` exclude_ids signature) +
``scrub_authored_document`` callable shipped in Phase 1 commit (i).

Gap 3 closes a latent race between cascade soft-delete and the
content_hash invalidation walk: the hook fires *before* victims'
``deleted_at`` is set, so a naive walk would issue UPDATE on the
victims themselves; if the soft-delete write happens in the same
flush (which it does — same transaction), the soft-delete protection
trigger trips on the next mutation in flight. Plumbing the victim
id-set through to ``ContentHashService.invalidate_subtree``'s
``exclude_ids`` kw-only parameter (already shipped in 0.2) skips
the doomed UPDATE while still walking past victims to recompute
their (surviving) parents.

The scrub-callable lives in ``cascade.py`` because it is wired by
the same engine that fires the cascade — the scrub is a one-shot
mutation on the root entity that must land in the same flush as the
``deleted_at`` write. Unit tests here lock the field-level
expectations from PHASE.md §1.3 (``filename`` → ``NULL``,
``source_url`` → ``""``).

Both invariants land alongside production code in commit (i) per
rule #13 — atomic commits over artificial bisect-separation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from course_supporter.config import get_settings
from course_supporter.storage.cascade import (
    CascadeDeleteService,
    OnInvalidateHashes,
    build_cascade_map,
    scrub_authored_document,
)
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Tenant,
)

# ── Unit-level: scrub_authored_document field semantics ───────────


class TestScrubAuthoredDocument:
    """Per PHASE.md §1.3, ``AuthoredDocument`` carries two scrub
    fields: ``filename`` (nullable) → ``NULL``, ``source_url``
    (NOT NULL) → ``""``.
    """

    async def test_scrub_clears_filename_to_null(self) -> None:
        doc = _StubDocument(
            filename="lecture-1-secrets.md",
            source_url="https://bucket/tenants/abc/files/123.md",
        )

        await scrub_authored_document(doc)

        assert doc.filename is None

    async def test_scrub_replaces_source_url_with_empty_string(self) -> None:
        doc = _StubDocument(
            filename="something.md",
            source_url="https://bucket/tenants/abc/files/123.md",
        )

        await scrub_authored_document(doc)

        # Empty string — NOT a magic sentinel like ``"[scrubbed]"``.
        # See ``scrub_authored_document`` docstring for rationale.
        assert doc.source_url == ""

    async def test_scrub_idempotent_on_already_null_filename(self) -> None:
        doc = _StubDocument(filename=None, source_url="https://example.com")

        await scrub_authored_document(doc)

        assert doc.filename is None
        assert doc.source_url == ""

    async def test_scrub_does_not_touch_unrelated_attributes(self) -> None:
        """Scrub list is exactly ``filename`` + ``source_url`` — other
        attributes (``id``, ``course_node_id``, ``source_type``, etc.)
        survive untouched so the soft-deleted row remains diagnosable
        via FK references and audit lookups.
        """
        original_id = uuid.uuid4()
        original_node_id = uuid.uuid4()
        doc = _StubDocument(
            id=original_id,
            course_node_id=original_node_id,
            source_type="video",
            filename="x.mp4",
            source_url="https://example.com/x.mp4",
        )

        await scrub_authored_document(doc)

        assert doc.id == original_id
        assert doc.course_node_id == original_node_id
        assert doc.source_type == "video"


class _StubDocument:
    """Minimal stand-in matching the AuthoredDocument scrub surface.

    Avoids spinning up the ORM mapper for what is purely a field-
    mutation test — a pure dataclass-shape stub is enough to verify
    that the scrub callable mutates exactly the expected attributes.
    """

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        course_node_id: uuid.UUID | None = None,
        source_type: str = "text",
        filename: str | None = None,
        source_url: str = "",
    ) -> None:
        self.id = id or uuid.uuid4()
        self.course_node_id = course_node_id or uuid.uuid4()
        self.source_type = source_type
        self.filename = filename
        self.source_url = source_url


# ── Type-level: OnInvalidateHashes signature contract ────────────


class TestOnInvalidateHashesSignatureContract:
    """The cascade engine forwards a ``set[UUID]`` of victim ids as
    the second positional argument so hook implementations can plumb
    it through to :meth:`ContentHashService.invalidate_subtree`'s
    kw-only ``exclude_ids`` parameter.
    """

    async def test_callable_alias_accepts_two_positional_args(self) -> None:
        """A function matching the new signature is assignable to the
        ``OnInvalidateHashes`` alias and callable with two positional
        args. This is a contract probe — if the alias regresses to a
        single-arg shape, this test fails at the assignment line.
        """

        async def hook(ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]) -> None:
            assert isinstance(ids, list)
            assert isinstance(exclude_ids, set)

        bound: OnInvalidateHashes = hook  # type-system probe
        await bound([uuid.uuid4()], {uuid.uuid4()})


# ── Integration: cascade fires hook with exclude_ids = victim ids ──


@pytest.fixture(scope="module")
async def gap3_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped engine for the Gap 3 regression integration test.

    Bound to the test database from settings — the soft-delete
    protection trigger (``block_update_on_soft_deleted``) is a real
    plpgsql trigger and only the live PostgreSQL dialect exercises
    its trip semantics.
    """
    engine = create_async_engine(
        get_settings().database_url,
        pool_size=2,
        max_overflow=0,
    )
    yield engine
    await engine.dispose()


@pytest.fixture()
def gap3_session_factory(
    gap3_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        gap3_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _make_tenant(session: AsyncSession, suffix: str) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name=f"gap3-{suffix}-{uuid.uuid4()}",
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
    async with session_factory() as session:
        for tid in tenant_ids:
            # Authored documents first (FK to course_nodes); then nodes;
            # then tenant. Bypassing soft-delete by issuing physical
            # DELETE is safe in cleanup since the test data is throw-
            # away — no production observers depend on retention.
            await session.execute(
                delete(AuthoredDocument).where(
                    AuthoredDocument.course_root_id.in_(
                        select(CourseNode.id).where(CourseNode.tenant_id == tid)
                    )
                )
            )
            await session.execute(delete(CourseNode).where(CourseNode.tenant_id == tid))
            await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


@pytest.mark.requires_db
class TestGap3ExcludeIdsRegression:
    """Cascade soft-delete completes without the
    ``block_update_on_soft_deleted`` trigger raising — the
    invalidation walk must skip UPDATE on victims via ``exclude_ids``.
    """

    async def test_cascade_soft_delete_does_not_trip_trigger(
        self,
        gap3_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Build a 3-level CourseNode tree, soft-delete the root via
        ``CascadeDeleteService`` with the real ``ContentHashService.
        invalidate_subtree`` bound as the hook. With Gap 3 in place
        the cascade flushes cleanly. Without it, the parent-hash
        walk would issue UPDATE on the victims and the next flush
        write would trip the trigger.
        """
        async with gap3_session_factory() as session:
            tenant = await _make_tenant(session, "trigger-trip")
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
            tenant_id = tenant.id
            root_id, mid_id, leaf_id = root.id, mid.id, leaf.id

        async with gap3_session_factory() as session:
            cascade_service = CascadeDeleteService(session)
            content_hash_service = ContentHashService(session)
            cascade_map = build_cascade_map(CourseNode)

            async def invalidate_hook(
                ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
            ) -> None:
                await content_hash_service.invalidate_subtree(
                    ids, exclude_ids=exclude_ids
                )

            # Refetch root in the new session so it is attached.
            root_attached = await session.get(CourseNode, root_id)
            assert root_attached is not None

            # Cascade should complete without raising. If Gap 3
            # regressed (signature mismatch or missing exclude_ids
            # forward), one of:
            #   - TypeError: hook signature mismatch, OR
            #   - DBAPIError wrapping the trigger's RAISE EXCEPTION
            # would surface here.
            await cascade_service.soft_delete_with_cascade(
                root_attached,
                cascade_map,
                on_invalidate_hashes=invalidate_hook,
            )
            await session.commit()

        # Assert all three nodes are soft-deleted (cascade reached
        # all levels) and survived the flush.
        async with gap3_session_factory() as session:
            for node_id in (root_id, mid_id, leaf_id):
                node = await session.get(CourseNode, node_id)
                assert node is not None, f"Node {node_id} unexpectedly hard-deleted"
                assert node.deleted_at is not None, (
                    f"Node {node_id} not marked soft-deleted"
                )

        await _cleanup_tenant(gap3_session_factory, [tenant_id])

    async def test_hook_receives_victim_id_set_as_second_arg(
        self,
        gap3_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Lock the engine-level contract: cascade engine forwards
        ``set(victim_ids)`` as the hook's second positional argument.
        Mirrors the unit-level mock test in
        ``tests/unit/test_cascade_delete_service.py`` but against the
        live cascade engine (no ``StubCascadeService``).
        """
        async with gap3_session_factory() as session:
            tenant = await _make_tenant(session, "hook-args")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            await session.commit()
            tenant_id = tenant.id
            root_id, child_id = root.id, child.id

        captured: dict[str, Any] = {}

        async def capture_hook(
            ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
        ) -> None:
            captured["ids"] = list(ids)
            captured["exclude_ids"] = exclude_ids

        async with gap3_session_factory() as session:
            cascade_service = CascadeDeleteService(session)
            cascade_map = build_cascade_map(CourseNode)
            root_attached = await session.get(CourseNode, root_id)
            assert root_attached is not None
            await cascade_service.soft_delete_with_cascade(
                root_attached,
                cascade_map,
                on_invalidate_hashes=capture_hook,
            )
            await session.commit()

        assert sorted(captured["ids"]) == sorted([root_id, child_id])
        assert captured["exclude_ids"] == {root_id, child_id}

        await _cleanup_tenant(gap3_session_factory, [tenant_id])


@pytest.mark.requires_db
class TestCascadeScrubCompleteness:
    """models-fix-3 integration: cascade soft-delete from a CourseNode
    root scrubs ALL victim types declaring ``__scrub_callable__`` —
    locks PHASE.md §3.2 step 24 (descendant CourseNode title scrubbed)
    + step 25 (descendant AuthoredDocument filename + source_url
    scrubbed) at the engine integration level.
    """

    async def test_multilevel_cascade_scrubs_all_victim_types(
        self,
        gap3_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Build CourseNode root → mid → leaf with an AuthoredDocument
        attached under ``mid``. Cascade soft-delete from root via
        ``CascadeDeleteService``. Assert every CourseNode has its
        ``title`` scrubbed to ``""`` + ``description`` to ``NULL`` AND
        the AuthoredDocument has ``filename`` to ``NULL`` + ``source_url``
        to ``""``. All four rows soft-deleted.
        """
        async with gap3_session_factory() as session:
            tenant = await _make_tenant(session, "scrub-completeness")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            mid = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="mid"
            )
            leaf = await _make_node(
                session, tenant_id=tenant.id, parent_id=mid.id, title="leaf"
            )
            doc = AuthoredDocument(
                id=uuid.uuid4(),
                course_node_id=mid.id,
                course_root_id=root.id,
                source_type="text",
                source_url="https://bucket/tenants/x/files/doc.md",
                filename="doc.md",
            )
            mid.description = "mid-description"
            doc.filename = "doc.md"
            session.add(doc)
            await session.flush()
            await session.commit()
            tenant_id = tenant.id
            root_id, mid_id, leaf_id, doc_id = root.id, mid.id, leaf.id, doc.id

        async with gap3_session_factory() as session:
            cascade_service = CascadeDeleteService(session)
            cascade_map = build_cascade_map(CourseNode)
            content_hash_service = ContentHashService(session)

            async def invalidate_hook(
                ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
            ) -> None:
                await content_hash_service.invalidate_subtree(
                    ids, exclude_ids=exclude_ids
                )

            root_attached = await session.get(CourseNode, root_id)
            assert root_attached is not None
            await cascade_service.soft_delete_with_cascade(
                root_attached,
                cascade_map,
                on_invalidate_hashes=invalidate_hook,
            )
            await session.commit()

        # All three CourseNodes scrubbed + soft-deleted.
        async with gap3_session_factory() as session:
            for node_id in (root_id, mid_id, leaf_id):
                node = await session.get(CourseNode, node_id)
                assert node is not None
                assert node.deleted_at is not None
                assert node.title == "", (
                    f"CourseNode {node_id} title not scrubbed: {node.title!r}"
                )
                assert node.description is None, (
                    f"CourseNode {node_id} description not scrubbed: "
                    f"{node.description!r}"
                )

            # AuthoredDocument descendant scrubbed + soft-deleted.
            persisted_doc = await session.get(AuthoredDocument, doc_id)
            assert persisted_doc is not None
            assert persisted_doc.deleted_at is not None
            assert persisted_doc.filename is None, (
                f"AuthoredDocument filename not scrubbed: {persisted_doc.filename!r}"
            )
            assert persisted_doc.source_url == "", (
                f"AuthoredDocument source_url not scrubbed: "
                f"{persisted_doc.source_url!r}"
            )

        await _cleanup_tenant(gap3_session_factory, [tenant_id])
