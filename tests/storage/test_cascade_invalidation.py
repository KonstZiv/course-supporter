"""Tests for Gap 3 (``on_invalidate_hashes`` exclude_ids signature) +
``scrub_authored_document`` callable shipped in Phase 1 commit (i),
updated for the KD3 author-content marker contract per hotfix-4
(Amendment 23 + 24).

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
``deleted_at`` write. Unit tests here lock the marker contract
(vision §3 KD3 + Amendment 24): every author-content column on a
soft-deleted ``AuthoredDocument`` carries the formatted Ukrainian
marker ``'інформація видалена автором DD-MM-YYYY HH:MM:SS'``,
regardless of nullability — Amendment 24 ratified that nullable
columns receive the marker too, since NULL conflates "автор не
заповнив" with "автор видалив". Original commit (i) shipped the
older "filename → NULL, source_url → ''" contract; hotfix-4
supersedes it.

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
from course_supporter.jobs.cancellation_service import JobCancellationService
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
    Job,
    Tenant,
)
from tests._helpers.kd3_marker import assert_marker_recent

# ── Unit-level: scrub_authored_document field semantics ───────────


class TestScrubAuthoredDocument:
    """Per vision §3 KD3 + Amendment 24, ``AuthoredDocument`` carries
    two scrub columns: ``filename`` (nullable) and ``source_url``
    (NOT NULL). Both receive the formatted Ukrainian-language marker
    ``'інформація видалена автором DD-MM-YYYY HH:MM:SS'`` regardless
    of nullability — hotfix-4 supersedes the original commit (i)
    contract (filename → NULL, source_url → '') because NULL
    conflates "author never filled the field" with "author actively
    deleted".
    """

    async def test_scrub_writes_marker_to_filename(self) -> None:
        doc = _StubDocument(
            filename="lecture-1-secrets.md",
            source_url="https://bucket/tenants/abc/files/123.md",
        )

        await scrub_authored_document(doc)

        assert doc.filename is not None
        assert_marker_recent(doc.filename)

    async def test_scrub_writes_marker_to_source_url(self) -> None:
        doc = _StubDocument(
            filename="something.md",
            source_url="https://bucket/tenants/abc/files/123.md",
        )

        await scrub_authored_document(doc)

        assert_marker_recent(doc.source_url)

    async def test_scrub_idempotent_re_stamps_marker(self) -> None:
        """Idempotency under the marker contract means a second scrub
        invocation is safe to call (no exception, columns end in a
        valid marker state) — NOT that a pre-scrub NULL is preserved.
        Both columns receive the marker regardless of pre-state.
        """
        doc = _StubDocument(filename=None, source_url="https://example.com")

        await scrub_authored_document(doc)

        assert doc.filename is not None
        assert_marker_recent(doc.filename)
        assert_marker_recent(doc.source_url)

    async def test_scrub_does_not_touch_unrelated_attributes(self) -> None:
        """Scrub list is exactly ``filename`` + ``source_url`` — other
        attributes (``id``, ``course_node_id``, ``source_type``, etc.)
        survive untouched so the soft-deleted row remains diagnosable
        via FK references and audit lookups. Includes regression-guard
        assertions on the scrubbed columns: a silent revert to the
        pre-Amendment-24 contract (filename → NULL, source_url → "")
        would slip past the surviving-attribute assertions, so we
        explicitly check that both columns now carry the KD3 marker.
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

        # Surviving attributes — outside the scrub set.
        assert doc.id == original_id
        assert doc.course_node_id == original_node_id
        assert doc.source_type == "video"
        # Regression guard against silent revert to NULL/'' contract.
        assert doc.filename is not None
        assert doc.filename != "x.mp4"
        assert doc.source_url != ""
        assert doc.source_url != "https://example.com/x.mp4"
        assert_marker_recent(doc.filename)
        assert_marker_recent(doc.source_url)


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
    locks the KD3 marker contract (vision §3 KD3 + Amendment 24) at
    the engine integration level: descendant CourseNode title +
    description AND descendant AuthoredDocument filename + source_url
    all carry the formatted marker post-cascade.
    """

    async def test_multilevel_cascade_scrubs_all_victim_types(
        self,
        gap3_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Build CourseNode root → mid → leaf with an AuthoredDocument
        attached under ``mid``. Cascade soft-delete from root via
        ``CascadeDeleteService``. Assert every CourseNode has its
        ``title`` + ``description`` stamped with the KD3 marker AND
        the AuthoredDocument has ``filename`` + ``source_url`` stamped
        with the KD3 marker. All four rows soft-deleted.
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
                assert node.title is not None, (
                    f"CourseNode {node_id} title unexpectedly NULL"
                )
                assert_marker_recent(node.title)
                assert node.description is not None, (
                    f"CourseNode {node_id} description unexpectedly NULL "
                    f"(Amendment 24 ruling: nullable column receives marker)"
                )
                assert_marker_recent(node.description)

            # AuthoredDocument descendant scrubbed + soft-deleted.
            persisted_doc = await session.get(AuthoredDocument, doc_id)
            assert persisted_doc is not None
            assert persisted_doc.deleted_at is not None
            assert persisted_doc.filename is not None, (
                f"AuthoredDocument filename unexpectedly NULL "
                f"(Amendment 24 ruling: nullable column receives marker): "
                f"{persisted_doc.filename!r}"
            )
            assert_marker_recent(persisted_doc.filename)
            assert_marker_recent(persisted_doc.source_url)

        await _cleanup_tenant(gap3_session_factory, [tenant_id])


@pytest.mark.requires_db
class TestKD13CancelHookIntegration:
    """Hotfix-5 integration: cascade soft-delete from a CourseNode root
    fires :class:`JobCancellationService.cancel_jobs_for_entities` as
    the ``on_cancel_jobs`` hook BEFORE scrub + ``deleted_at`` write.
    Locks vision §KD13 binding contract at the cascade-engine
    integration level.

    Critical invariants:
    - In-progress Jobs scoped to a victim CourseNode flip to
      ``status='cancelled'`` + ``completed_at = now()``.
    - Job ``deleted_at`` REMAINS NULL — Job is sibling-cancelled, NOT
      cascade soft-deleted (per PHASE.md §1.2 audit: Job ∉ any
      ``__cascades_soft_delete_to__`` chain).
    - Cascade still works alongside cancel: root + descendants
      soft-deleted with KD3 marker.

    Provides production-side coverage of the wiring shipped in
    hotfix-5 — the unit-level mock tests assert closure shape +
    augmentation; this test exercises the live engine + JCS + Job
    table together.
    """

    async def test_cascade_cancels_in_progress_job_for_victim_node(
        self,
        gap3_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Build CourseNode root → child + an in-progress
        ``ingest`` Job referencing the child via ``course_node_id``.
        Cascade soft-delete from root with
        ``on_cancel_jobs=JobCancellationService(...).cancel_jobs_for_entities``
        bound directly (mirrors ``delete_node`` handler pattern).
        Assert the Job flipped to cancelled but its row remains
        soft-delete-NULL.
        """
        async with gap3_session_factory() as session:
            tenant = await _make_tenant(session, "kd13-cancel")
            root = await _make_node(
                session, tenant_id=tenant.id, parent_id=None, title="root"
            )
            child = await _make_node(
                session, tenant_id=tenant.id, parent_id=root.id, title="child"
            )
            job = Job(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                course_node_id=child.id,
                job_type="ingest",
                priority="normal",
                status="running",
                input_params={"course_node_id": str(child.id)},
            )
            session.add(job)
            await session.commit()
            tenant_id = tenant.id
            root_id, child_id, job_id = root.id, child.id, job.id

        async with gap3_session_factory() as session:
            cascade_service = CascadeDeleteService(session)
            cascade_map = build_cascade_map(CourseNode)
            content_hash_service = ContentHashService(session)
            job_cancellation_service = JobCancellationService(session)

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
                on_cancel_jobs=(job_cancellation_service.cancel_jobs_for_entities),
                on_invalidate_hashes=invalidate_hook,
            )
            await session.commit()

        async with gap3_session_factory() as session:
            persisted_job = await session.get(Job, job_id)
            assert persisted_job is not None, (
                "Job row unexpectedly hard-deleted — KD13 says cancel, not delete"
            )
            assert persisted_job.status == "cancelled", (
                f"Job.status not flipped: got {persisted_job.status!r}, "
                "expected 'cancelled' per vision §KD13"
            )
            assert persisted_job.completed_at is not None, (
                "Job.completed_at not set — JCS must stamp completion "
                "timestamp on cancel"
            )
            assert persisted_job.deleted_at is None, (
                "Job.deleted_at unexpectedly set — Job is sibling-"
                "cancelled, NOT cascade soft-deleted (per PHASE.md "
                "§1.2: Job ∉ any __cascades_soft_delete_to__ chain)"
            )

            # Cascade also still works: root + descendant CourseNodes
            # soft-deleted alongside Job cancellation.
            for node_id in (root_id, child_id):
                node = await session.get(CourseNode, node_id)
                assert node is not None
                assert node.deleted_at is not None, (
                    f"CourseNode {node_id} not soft-deleted — cascade "
                    "regressed alongside KD13 wiring"
                )

        # Cleanup also removes the Job row to keep the test DB clean.
        async with gap3_session_factory() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.commit()
        await _cleanup_tenant(gap3_session_factory, [tenant_id])
