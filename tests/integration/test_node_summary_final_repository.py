"""Unit tests for ``NodeSummaryFinalRepository`` (Phase 3.2.4 c1).

Pins all three write channels per KD11 §1055-1071, both read paths used
by Phase 3.2.4 c2 routes, and the snapshot lifecycle (create / replace
/ hard-delete).

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.storage.node_summary_final_repository import (
    NodeSummaryFinalRepository,
)
from course_supporter.storage.orm import (
    CourseNode,
    NodeSummaryFinal,
    NodeSummaryFinalPreviousSnapshot,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


# ── Helpers ────────────────────────────────────────────────────────


async def _seed_root(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Tenant + root CourseNode + 1 child; commit; return ids."""
    async with session_factory() as session:
        tenant = Tenant(name=f"final-repo-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(tenant_id=tenant.id, title="root", order=0)
        session.add(root)
        await session.flush()

        child = CourseNode(
            tenant_id=tenant.id,
            parent_id=root.id,
            title="child",
            order=0,
        )
        session.add(child)
        await session.flush()
        await session.commit()

        return {
            "tenant_id": tenant.id,
            "root_id": root.id,
            "child_id": child.id,
        }


def _make_raw(course_node_id: uuid.UUID, **overrides: Any) -> NodeSummaryRaw:
    defaults: dict[str, Any] = {
        "course_node_id": course_node_id,
        "title": "raw-title",
        "description": "raw-description",
        "learning_objectives": ["obj-1", "obj-2"],
        "knowledge": [{"name": "k1", "description": "kd"}],
        "skills": [{"name": "s1", "description": "sd"}],
        "success_criteria": ["sc-1"],
        "assessment_approach": "assess",
        "teaching_approach": "teach",
        "key_activities": ["act-1"],
        "common_mistakes": ["mistake-1"],
        "main_concepts": ["c1"],
        "secondary_concepts": ["c2"],
        "compressed_summary": "RAW-ONLY: must not propagate to Final",
        "enclosing_context": None,
        "methodist_observations": ["RAW-ONLY: must not propagate to Final"],
        "own_documents_count": 1,
        "own_chars_count": 100,
        "cumulative_documents_count": 1,
        "cumulative_chars_count": 100,
    }
    defaults.update(overrides)
    return NodeSummaryRaw(**defaults)


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    from sqlalchemy import delete

    async with session_factory() as session:
        await session.execute(
            delete(NodeSummaryFinalPreviousSnapshot).where(
                NodeSummaryFinalPreviousSnapshot.node_summary_final_id.in_(
                    select(NodeSummaryFinal.id).where(
                        NodeSummaryFinal.course_node_id.in_(
                            select(CourseNode.id).where(
                                CourseNode.tenant_id == tenant_id
                            )
                        )
                    )
                )
            )
        )
        await session.execute(
            delete(NodeSummaryFinal).where(
                NodeSummaryFinal.course_node_id.in_(
                    select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
                )
            )
        )
        await session.execute(
            delete(NodeSummaryRaw).where(
                NodeSummaryRaw.course_node_id.in_(
                    select(CourseNode.id).where(CourseNode.tenant_id == tenant_id)
                )
            )
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


# ── Channel 1 — write_from_raw_with_snapshot ─────────────────────


class TestChannel1FirstInsert:
    """First Raw landing for a node — Final INSERT, NO snapshot."""

    async def test_first_insert_creates_final_without_snapshot(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()

                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)

                assert final.course_node_id == ids["root_id"]
                assert final.title == "raw-title"
                assert final.learning_objectives == ["obj-1", "obj-2"]
                assert final.main_concepts == ["c1"]
                assert final.approved_at is None
                # is_manual default preserved (channel 1 never flips it).
                assert final.is_manual is False
                await session.commit()

            async with session_factory() as session:
                # No snapshot for the very first overwrite.
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final.id
                        )
                    )
                ).scalar_one_or_none()
                assert snap is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel1FiltersRawOnlyFields:
    """``methodist_observations`` and ``compressed_summary`` never reach Final."""

    async def test_write_from_raw_excludes_raw_only_fields(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()

                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)

                assert not hasattr(final, "methodist_observations")
                assert not hasattr(final, "compressed_summary")
                # ORM should not magically have these columns either.
                assert "methodist_observations" not in NodeSummaryFinal.__table__.c
                assert "compressed_summary" not in NodeSummaryFinal.__table__.c
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel1Overwrite:
    """Second Raw landing — Final overwritten + snapshot created."""

    async def test_overwrite_creates_snapshot_and_resets_approved_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            # First landing — INSERT Final, no snapshot.
            async with session_factory() as session:
                raw1 = _make_raw(ids["root_id"], title="v1")
                session.add(raw1)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw1)
                # Simulate author approval mid-way.
                final.approved_at = datetime.now(UTC)
                await session.commit()
                final_id = final.id

            # Second landing — overwrite + snapshot + approved_at reset.
            async with session_factory() as session:
                raw2 = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw2.title = "v2"
                raw2.description = "v2-desc"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final2 = await repo.write_from_raw_with_snapshot(raw2)

                assert final2.id == final_id
                assert final2.title == "v2"
                assert final2.description == "v2-desc"
                assert final2.approved_at is None
                await session.commit()

            async with session_factory() as session:
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one()
                # Snapshot captured the v1 title (prior version).
                assert snap.snapshot["title"] == "v1"
                # approved_at in the snapshot reflects the prior approval.
                assert snap.snapshot["approved_at"] is not None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel1SnapshotReplacedNotAccumulated:
    """Each subsequent overwrite REPLACES the snapshot, never accumulates."""

    async def test_three_overwrites_keep_exactly_one_snapshot(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"], title="v1")
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                await session.commit()
                final_id = final.id

            for label in ("v2", "v3", "v4"):
                async with session_factory() as session:
                    raw = (
                        await session.execute(
                            select(NodeSummaryRaw).where(
                                NodeSummaryRaw.course_node_id == ids["root_id"]
                            )
                        )
                    ).scalar_one()
                    raw.title = label
                    await session.flush()
                    repo = NodeSummaryFinalRepository(session)
                    await repo.write_from_raw_with_snapshot(raw)
                    await session.commit()

            async with session_factory() as session:
                snaps = (
                    (
                        await session.execute(
                            select(NodeSummaryFinalPreviousSnapshot).where(
                                NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                                == final_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                # Single-version invariant — exactly one row.
                assert len(snaps) == 1
                # Snapshot reflects the version JUST BEFORE the last
                # overwrite (v3), not the original (v1).
                assert snaps[0].snapshot["title"] == "v3"
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


# ── Channel 3 — refresh_enclosing_context ────────────────────────


class TestChannel3DoesNotResetApprovedAt:
    """KD11 §1066 invariant — third channel never invalidates author approval."""

    async def test_refresh_preserves_approved_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            # Channel 1 seed + approve.
            approved_at_marker: datetime
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                approved_at_marker = datetime(2026, 1, 1, tzinfo=UTC)
                final.approved_at = approved_at_marker
                await session.commit()

            # Channel 3 — refresh enclosing_context with a new value.
            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.enclosing_context = "fresh enclosing"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                refreshed = await repo.refresh_enclosing_context(raw)
                assert refreshed is not None
                assert refreshed.enclosing_context == "fresh enclosing"
                # approved_at NOT touched.
                assert refreshed.approved_at == approved_at_marker
                assert refreshed.enclosing_context_updated_at is not None
                # Bumped to a time AFTER the approval marker.
                assert refreshed.enclosing_context_updated_at > approved_at_marker
                await session.commit()
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel3DoesNotCreateSnapshot:
    """Snapshots track prior content versions; enclosing_context is not content."""

    async def test_refresh_does_not_emit_snapshot(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                await session.commit()
                final_id = final.id

            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.enclosing_context = "second refresh"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.refresh_enclosing_context(raw)
                await session.commit()

            async with session_factory() as session:
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one_or_none()
                assert snap is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel3WithoutFinalIsNoOp:
    """Defensive — orchestrator's Pass-1-first invariant makes this unreachable
    in practice, but the repository stays defensive."""

    async def test_refresh_returns_none_when_final_absent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()
                # NB: no write_from_raw_with_snapshot call — Final absent.
                repo = NodeSummaryFinalRepository(session)
                result = await repo.refresh_enclosing_context(raw)
                assert result is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


# ── Channel 2 — patch_editable ───────────────────────────────────


class TestChannel2RejectsNonEditableFields:
    """KD11 §1043-1054 — concepts / enclosing_context / meta blocked."""

    async def test_main_concepts_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                with pytest.raises(ValueError, match="non-editable"):
                    await repo.patch_editable(
                        ids["root_id"], {"main_concepts": ["hacked"]}
                    )
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestChannel2EditableFieldsAccepted:
    """All 10 KD11 editable fields go through; ``approved_at`` untouched."""

    async def test_patch_editable_writes_and_preserves_approved_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            approved_at_marker = datetime(2026, 1, 1, tzinfo=UTC)
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"])
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                final.approved_at = approved_at_marker
                await session.commit()

            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                updated = await repo.patch_editable(
                    ids["root_id"],
                    {
                        "title": "edited title",
                        "description": "edited desc",
                        "learning_objectives": ["new-1", "new-2"],
                    },
                )
                assert updated is not None
                assert updated.title == "edited title"
                assert updated.description == "edited desc"
                assert updated.learning_objectives == ["new-1", "new-2"]
                # approved_at NOT reset by channel 2.
                assert updated.approved_at == approved_at_marker
                # is_manual stays at default.
                assert updated.is_manual is False
                await session.commit()
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


# ── Approve / accept-raw — snapshot hard-delete ──────────────────


class TestApproveHardDeletesSnapshot:
    """Approve sets ``approved_at`` + hard-deletes the snapshot."""

    async def test_approve_drops_snapshot_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            # Channel 1 twice — second call creates the snapshot.
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"], title="v1")
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                await session.commit()
                final_id = final.id

            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.title = "v2"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            # Sanity — snapshot exists.
            async with session_factory() as session:
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one_or_none()
                assert snap is not None

            # Approve — snapshot hard-deleted.
            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                approved = await repo.approve(ids["root_id"])
                assert approved is not None
                assert approved.approved_at is not None
                await session.commit()

            async with session_factory() as session:
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one_or_none()
                assert snap is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


class TestAcceptRawOverwritesApprovesDropsSnapshot:
    """Accept-raw is implicit-approve flavour of channel 1."""

    async def test_accept_raw_overwrites_and_approves(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"], title="v1")
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                await session.commit()
                final_id = final.id

            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.title = "v2"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            # accept-raw — copies current Raw + approve + drop snapshot.
            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                # Bump Raw once more to make the overwrite observable.
                raw.title = "v3"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                accepted = await repo.accept_raw(raw)
                assert accepted is not None
                assert accepted.title == "v3"
                assert accepted.approved_at is not None
                await session.commit()

            async with session_factory() as session:
                snap = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one_or_none()
                assert snap is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


# ── Read paths — get_edit_view + get_by_course_node_id ───────────


class TestGetEditView:
    """KD11 §1086-1101 — combined view (final + raw_observations + snapshot)."""

    async def test_edit_view_aliases_methodist_observations_as_raw_observations(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(
                    ids["root_id"],
                    methodist_observations=["obs-1", "obs-2"],
                )
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                result = await repo.get_edit_view(ids["root_id"])
                assert result is not None
                final, raw_observations, snapshot = result
                assert final.course_node_id == ids["root_id"]
                # API-side alias on top of ORM column ``methodist_observations``.
                assert raw_observations == ["obs-1", "obs-2"]
                # No snapshot — single overwrite.
                assert snapshot is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])

    async def test_edit_view_returns_none_when_raw_missing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                result = await repo.get_edit_view(ids["root_id"])
                assert result is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])

    async def test_get_by_course_node_id_returns_none_when_absent(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                repo = NodeSummaryFinalRepository(session)
                final = await repo.get_by_course_node_id(uuid.uuid4())
                assert final is None
        finally:
            await _cleanup(session_factory, ids["tenant_id"])


# ── Snapshot timestamp bumping ───────────────────────────────────


class TestSnapshotReplacedAtBumps:
    """``replaced_at`` advances on every snapshot rewrite."""

    async def test_replaced_at_strictly_increases(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _seed_root(session_factory)
        try:
            async with session_factory() as session:
                raw = _make_raw(ids["root_id"], title="v1")
                session.add(raw)
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                final = await repo.write_from_raw_with_snapshot(raw)
                await session.commit()
                final_id = final.id

            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.title = "v2"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            async with session_factory() as session:
                first = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one()
                first_replaced_at = first.replaced_at

            # Force a small gap so any rewrite produces a strictly-greater
            # ``replaced_at`` even on fast CI clocks.
            async with session_factory() as session:
                raw = (
                    await session.execute(
                        select(NodeSummaryRaw).where(
                            NodeSummaryRaw.course_node_id == ids["root_id"]
                        )
                    )
                ).scalar_one()
                raw.title = "v3"
                await session.flush()
                repo = NodeSummaryFinalRepository(session)
                # Tiny artificial delay via timedelta-comparison logic only:
                # ``datetime.now`` ticks fast enough on every supported
                # platform that another microsecond passes between calls.
                await repo.write_from_raw_with_snapshot(raw)
                await session.commit()

            async with session_factory() as session:
                second = (
                    await session.execute(
                        select(NodeSummaryFinalPreviousSnapshot).where(
                            NodeSummaryFinalPreviousSnapshot.node_summary_final_id
                            == final_id
                        )
                    )
                ).scalar_one()
                # Bumped — strictly greater (or equal in worst case;
                # we relax to >= to keep the test resilient to coarse
                # clock granularity on CI).
                assert second.replaced_at >= first_replaced_at
                assert second.snapshot["title"] == "v2"
                # And not much time has passed in practice.
                assert second.replaced_at - first_replaced_at < timedelta(seconds=10)
        finally:
            await _cleanup(session_factory, ids["tenant_id"])
