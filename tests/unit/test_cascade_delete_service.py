"""Unit tests for CascadeDeleteService (vision §3 KD3, KD12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from course_supporter.storage.cascade import (
    CascadeDeleteService,
    SoftDeletableEntity,
    build_cascade_map,
)

# ── Fakes: minimal soft-deletable entities (no SQLAlchemy involved) ─


class FakeEntity:
    """In-memory soft-deletable entity for engine-only testing."""

    def __init__(self, id_: uuid.UUID | None = None) -> None:
        self.id: uuid.UUID = id_ or uuid.uuid4()
        self.deleted_at: datetime | None = None


class A(FakeEntity):
    pass


class B(FakeEntity):
    pass


class C(FakeEntity):
    pass


class StubCascadeService(CascadeDeleteService):
    """``CascadeDeleteService`` with the FK-inspection layer stubbed out.

    Replaces ``_fetch_active_children`` with a static lookup table so
    we can drive the engine logic without real ORM mappers or a DB.
    """

    def __init__(
        self,
        session: Any,
        children_by_parent: dict[uuid.UUID, dict[type, list[FakeEntity]]],
    ) -> None:
        super().__init__(session)
        self._children = children_by_parent

    async def _fetch_active_children(  # type: ignore[override]
        self,
        parent: SoftDeletableEntity,
        child_cls: type,
    ) -> list[SoftDeletableEntity]:
        kids = self._children.get(parent.id, {}).get(child_cls, [])
        return [k for k in kids if k.deleted_at is None]


# ── build_cascade_map ──────────────────────────────────────────────


class TestBuildCascadeMap:
    def test_empty_root(self) -> None:
        class Root:
            pass

        assert build_cascade_map(Root) == {Root: []}

    def test_walks_transitive_descendants(self) -> None:
        class Leaf:
            pass

        class Mid:
            __cascades_soft_delete_to__: ClassVar[list[type]] = [Leaf]

        class Root:
            __cascades_soft_delete_to__: ClassVar[list[type]] = [Mid]

        cmap = build_cascade_map(Root)
        assert cmap == {Root: [Mid], Mid: [Leaf], Leaf: []}

    def test_deduplicates_shared_descendants(self) -> None:
        class Leaf:
            pass

        class Branch1:
            __cascades_soft_delete_to__: ClassVar[list[type]] = [Leaf]

        class Branch2:
            __cascades_soft_delete_to__: ClassVar[list[type]] = [Leaf]

        class Root:
            __cascades_soft_delete_to__: ClassVar[list[type]] = [Branch1, Branch2]

        cmap = build_cascade_map(Root)
        assert set(cmap.keys()) == {Root, Branch1, Branch2, Leaf}
        assert cmap[Leaf] == []


# ── soft_delete_with_cascade ───────────────────────────────────────


class TestCascadeIdempotency:
    async def test_already_deleted_root_short_circuits(self) -> None:
        session = AsyncMock()
        a = A()
        a.deleted_at = datetime(2026, 1, 1, tzinfo=UTC)
        hook = AsyncMock()

        svc = StubCascadeService(session, {})
        await svc.soft_delete_with_cascade(a, {A: []}, on_cancel_jobs=hook)

        hook.assert_not_awaited()
        session.flush.assert_not_awaited()


class TestCascadeDepth:
    async def test_traverses_depth_n_tree(self) -> None:
        a = A()
        b1, b2 = B(), B()
        c1, c2, c3 = C(), C(), C()

        children = {
            a.id: {B: [b1, b2]},
            b1.id: {C: [c1, c2]},
            b2.id: {C: [c3]},
        }
        cmap: dict[type, list[type]] = {A: [B], B: [C], C: []}

        session = AsyncMock()
        svc = StubCascadeService(session, children)
        ts = datetime(2026, 1, 1, tzinfo=UTC)

        await svc.soft_delete_with_cascade(a, cmap, now=ts)

        for e in (a, b1, b2, c1, c2, c3):
            assert e.deleted_at == ts
        session.flush.assert_awaited_once()


class TestCascadeHook:
    async def test_hook_called_once_with_all_collected_ids(self) -> None:
        a = A()
        b = B()
        children = {a.id: {B: [b]}}
        cmap: dict[type, list[type]] = {A: [B], B: []}

        session = AsyncMock()
        svc = StubCascadeService(session, children)
        hook = AsyncMock()

        await svc.soft_delete_with_cascade(a, cmap, on_cancel_jobs=hook)

        hook.assert_awaited_once()
        passed_ids = list(hook.call_args.args[0])
        assert sorted(passed_ids) == sorted([a.id, b.id])

    async def test_hook_runs_before_writes(self) -> None:
        """Hook must observe entities still NOT marked deleted."""
        a = A()
        cmap: dict[type, list[type]] = {A: []}
        session = AsyncMock()
        svc = StubCascadeService(session, {})

        observed: list[datetime | None] = []

        async def hook(_ids: list[uuid.UUID]) -> None:
            observed.append(a.deleted_at)

        await svc.soft_delete_with_cascade(a, cmap, on_cancel_jobs=hook)

        assert observed == [None]
        assert a.deleted_at is not None


class TestCascadeAllOrNothing:
    async def test_hook_failure_aborts_writes(self) -> None:
        """If on_cancel_jobs raises, entities stay un-mutated and no flush."""
        a = A()
        cmap: dict[type, list[type]] = {A: []}
        session = AsyncMock()
        svc = StubCascadeService(session, {})

        async def failing_hook(_ids: list[uuid.UUID]) -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await svc.soft_delete_with_cascade(a, cmap, on_cancel_jobs=failing_hook)

        assert a.deleted_at is None
        session.flush.assert_not_awaited()


class TestCascadeCycleGuard:
    async def test_cycle_does_not_infinite_loop(self) -> None:
        """A cycle in cascade_map must terminate via the visited set."""
        a = A()
        b = B()
        children = {a.id: {B: [b]}, b.id: {A: [a]}}
        cmap: dict[type, list[type]] = {A: [B], B: [A]}

        session = AsyncMock()
        svc = StubCascadeService(session, children)
        ts = datetime(2026, 1, 1, tzinfo=UTC)

        await svc.soft_delete_with_cascade(a, cmap, now=ts)

        assert a.deleted_at == ts
        assert b.deleted_at == ts
        session.flush.assert_awaited_once()
