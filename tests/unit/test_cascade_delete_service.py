"""Unit tests for CascadeDeleteService (vision §3 KD3, KD12)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

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

    Replaces ``_fetch_active_children_batch`` with a static lookup
    table so we can drive the engine logic without real ORM mappers
    or a DB. Real FK-resolution behaviour is exercised separately in
    ``TestRealFKResolution`` against actual ORM models.
    """

    def __init__(
        self,
        session: Any,
        children_by_parent: dict[uuid.UUID, dict[type, list[FakeEntity]]],
    ) -> None:
        super().__init__(session)
        self._children = children_by_parent

    async def _fetch_active_children_batch(  # type: ignore[override]
        self,
        parent_cls: type,
        parent_ids: list[uuid.UUID],
        child_cls: type,
    ) -> list[SoftDeletableEntity]:
        result: list[SoftDeletableEntity] = []
        for pid in parent_ids:
            kids = self._children.get(pid, {}).get(child_cls, [])
            result.extend(k for k in kids if k.deleted_at is None)
        return result


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


class TestCascadeInvalidateHook:
    """Mirrors ``TestCascadeHook`` / ``TestCascadeAllOrNothing`` for the
    ``on_invalidate_hashes`` hook added in task 0.2 (vision §3 KD9 + KD12).

    The hook contract is identical to ``on_cancel_jobs``: single-shot,
    pre-write, full id list, all-or-nothing on failure. Phase 1.x cascade
    wiring will bind ``ContentHashService.invalidate_subtree(ids,
    exclude_ids=ids)`` to it; here we verify the engine plumbing only.
    """

    async def test_hook_called_once_with_all_collected_ids(self) -> None:
        a = A()
        b = B()
        children = {a.id: {B: [b]}}
        cmap: dict[type, list[type]] = {A: [B], B: []}

        session = AsyncMock()
        svc = StubCascadeService(session, children)
        hook = AsyncMock()

        await svc.soft_delete_with_cascade(a, cmap, on_invalidate_hashes=hook)

        hook.assert_awaited_once()
        passed_ids = list(hook.call_args.args[0])
        assert sorted(passed_ids) == sorted([a.id, b.id])

    async def test_hook_runs_before_writes(self) -> None:
        """Hook must observe entities still NOT marked deleted (KD12 line 645)."""
        a = A()
        cmap: dict[type, list[type]] = {A: []}
        session = AsyncMock()
        svc = StubCascadeService(session, {})

        observed: list[datetime | None] = []

        async def hook(_ids: list[uuid.UUID]) -> None:
            observed.append(a.deleted_at)

        await svc.soft_delete_with_cascade(a, cmap, on_invalidate_hashes=hook)

        assert observed == [None]
        assert a.deleted_at is not None

    async def test_failure_aborts_writes(self) -> None:
        a = A()
        cmap: dict[type, list[type]] = {A: []}
        session = AsyncMock()
        svc = StubCascadeService(session, {})

        async def failing_hook(_ids: list[uuid.UUID]) -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await svc.soft_delete_with_cascade(
                a, cmap, on_invalidate_hashes=failing_hook
            )

        assert a.deleted_at is None
        session.flush.assert_not_awaited()

    async def test_both_hooks_fire_in_declared_order(self) -> None:
        """on_cancel_jobs first (stop work), then on_invalidate_hashes (recompute)."""
        a = A()
        cmap: dict[type, list[type]] = {A: []}
        session = AsyncMock()
        svc = StubCascadeService(session, {})

        order: list[str] = []

        async def cancel_hook(_ids: list[uuid.UUID]) -> None:
            order.append("cancel")

        async def invalidate_hook(_ids: list[uuid.UUID]) -> None:
            order.append("invalidate")

        await svc.soft_delete_with_cascade(
            a,
            cmap,
            on_cancel_jobs=cancel_hook,
            on_invalidate_hashes=invalidate_hook,
        )

        assert order == ["cancel", "invalidate"]


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


# ── Real FK resolution against actual ORM models ──────────────────


class TestRealFKResolution:
    """Drive ``_resolve_cascade_columns`` against real ORM models.

    ``StubCascadeService`` above bypasses ``_fetch_active_children_batch``
    entirely, so the real ``sqlalchemy.inspect()``-based FK lookup and
    the ambiguity guard would otherwise be untested. These tests fill
    that gap. Pure mapper inspection — no DB connection involved.
    """

    def setup_method(self) -> None:
        # Each test starts with an empty cache so behaviour is
        # observable per-call (matters for the caching test).
        from course_supporter.storage.cascade import _resolve_cascade_columns

        _resolve_cascade_columns.cache_clear()

    def test_resolves_unique_fk(self) -> None:
        """MaterialEntry -> MaterialMacroSection via material_entry_id."""
        from course_supporter.storage.cascade import _resolve_cascade_columns
        from course_supporter.storage.orm import (
            MaterialEntry,
            MaterialMacroSection,
        )

        fk_col, deleted_at_col = _resolve_cascade_columns(
            MaterialEntry, MaterialMacroSection
        )
        assert fk_col.name == "material_entry_id"
        assert deleted_at_col.name == "deleted_at"
        assert deleted_at_col.table.name == "material_macro_sections"

    def test_resolves_self_referential_fk(self) -> None:
        """MaterialNode -> MaterialNode via parent_materialnode_id."""
        from course_supporter.storage.cascade import _resolve_cascade_columns
        from course_supporter.storage.orm import MaterialNode

        fk_col, deleted_at_col = _resolve_cascade_columns(MaterialNode, MaterialNode)
        assert fk_col.name == "parent_materialnode_id"
        assert deleted_at_col.name == "deleted_at"

    def test_no_fk_raises_value_error(self) -> None:
        """MaterialMacroSection has no FK back to tenants."""
        from course_supporter.storage.cascade import _resolve_cascade_columns
        from course_supporter.storage.orm import MaterialMacroSection, Tenant

        with pytest.raises(
            ValueError,
            match=r"No foreign key from MaterialMacroSection to Tenant",
        ):
            _resolve_cascade_columns(Tenant, MaterialMacroSection)

    def test_ambiguous_fks_raise_value_error(self) -> None:
        """HomeworkSubmission has BOTH course_node_id AND node_id -> material_nodes.

        This is the actual code-path that will fire when phase 4 wires
        HomeworkSubmission's cascade. Surfacing it here so the failure
        is loud and early rather than silent and wrong.
        """
        from course_supporter.storage.cascade import _resolve_cascade_columns
        from course_supporter.storage.orm import HomeworkSubmission, MaterialNode

        with pytest.raises(ValueError, match=r"Ambiguous foreign keys") as excinfo:
            _resolve_cascade_columns(MaterialNode, HomeworkSubmission)
        # Both FK column names must be in the error so the operator can
        # decide which one to keep / split.
        msg = str(excinfo.value)
        assert "course_node_id" in msg
        assert "node_id" in msg

    def test_caching_returns_same_columns(self) -> None:
        """Second call hits the cache and returns the identical objects."""
        from course_supporter.storage.cascade import _resolve_cascade_columns
        from course_supporter.storage.orm import (
            MaterialEntry,
            MaterialMacroSection,
        )

        first = _resolve_cascade_columns(MaterialEntry, MaterialMacroSection)
        second = _resolve_cascade_columns(MaterialEntry, MaterialMacroSection)
        assert first is second  # `is` proves cache hit, not just equality


# ── Batched fetch wire-format ─────────────────────────────────────


class TestBatchedFetch:
    """Verify ``_fetch_active_children_batch`` issues a single
    ``SELECT ... WHERE fk IN (...)`` query rather than one per parent.

    Addresses the N+1 concern raised on the original implementation:
    the engine should scale with cascade depth, not with row count.
    """

    async def test_single_query_with_in_clause(self) -> None:
        from course_supporter.storage.orm import (
            MaterialEntry,
            MaterialMacroSection,
        )

        parent_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session = AsyncMock()
        session.execute.return_value = result_mock

        svc = CascadeDeleteService(session)
        rows = await svc._fetch_active_children_batch(
            MaterialEntry, parent_ids, MaterialMacroSection
        )

        assert rows == []
        session.execute.assert_awaited_once()  # one query, not three

        captured = session.execute.call_args.args[0]
        compiled = str(captured.compile(compile_kwargs={"literal_binds": True}))
        assert "material_entry_id IN" in compiled
        assert "deleted_at IS NULL" in compiled

    async def test_empty_parent_ids_short_circuits(self) -> None:
        """Empty parent_ids must not produce an empty-IN SQL syntax error."""
        from course_supporter.storage.orm import (
            MaterialEntry,
            MaterialMacroSection,
        )

        session = AsyncMock()
        svc = CascadeDeleteService(session)

        rows = await svc._fetch_active_children_batch(
            MaterialEntry, [], MaterialMacroSection
        )

        assert rows == []
        session.execute.assert_not_called()
