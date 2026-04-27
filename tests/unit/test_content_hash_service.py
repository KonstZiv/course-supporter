"""Unit tests for ContentHashService and compute helpers (vision §3 KD9).

Three groups of coverage:

1. **Pure compute helpers** (``compute_raw_hash`` / ``compute_content_hash``):
   determinism, order-independence, separator integrity (the
   ``["abc","def"]`` vs ``["abcd","ef"]`` collision pitfall from
   TASK.md), known-value check on SHA-256 hex.
2. **Walker engine** (``invalidate_up`` / ``_walk_up``): driven via a
   ``StubContentHashService`` that overrides the DB-touching methods
   so the engine logic is exercised without ORM or a database.
   Real-FK behaviour is covered by the integration test suite.
3. **Cascade-time exclude_ids semantics**: both the standalone
   walker and the bulk variant verified to forward ``exclude_ids``
   to the per-entity hash compute and to skip ``UPDATE`` on
   victim rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from course_supporter.storage.content_hash import (
    _MAX_WALK_DEPTH,
    ContentHashService,
    HashableEntity,
    compute_content_hash,
    compute_raw_hash,
)

# ── Fakes for engine-only walker testing ──────────────────────────


class FakeHashable:
    """In-memory hash-bearing entity (no SQLAlchemy involved)."""

    def __init__(
        self,
        id_: uuid.UUID | None = None,
        *,
        content_hash: str | None = None,
        deleted_at: datetime | None = None,
    ) -> None:
        self.id: uuid.UUID = id_ or uuid.uuid4()
        self.content_hash: str | None = content_hash
        self.deleted_at: datetime | None = deleted_at


class A(FakeHashable):
    pass


class B(FakeHashable):
    pass


class C(FakeHashable):
    pass


def _stable_hash_for(entity: FakeHashable) -> str:
    """Synthetic stable-per-(type, id) hash used by the default stub formula.

    Single source of truth: ``StubContentHashService._compute_hash_for``
    delegates here when no explicit ``hash_func`` override is given, and
    test assertions call the same function to recompute the expected
    value — no risk of the stub formula and the assertion helper
    drifting apart.
    """
    return compute_content_hash(
        f"{type(entity).__name__}:{entity.id}".encode(),
        [],
    )


class StubContentHashService(ContentHashService):
    """``ContentHashService`` with the per-entity dispatch stubbed out.

    Replaces ``_compute_hash_for`` with a synthetic deterministic hash
    function (or caller-supplied override) and ``_fetch_parent`` with
    a static lookup table. Real ORM / FK behaviour is covered by the
    integration test suite — this stub exists so the walker engine is
    testable in isolation.
    """

    def __init__(
        self,
        session: Any,
        *,
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] | None = None,
        hash_func: Any = None,
    ) -> None:
        super().__init__(session)
        self._parent_map = parent_map or {}
        self._hash_func = hash_func
        self.compute_calls: list[tuple[type, uuid.UUID, set[uuid.UUID] | None]] = []

    async def _fetch_parent(  # type: ignore[override]
        self, entity: HashableEntity
    ) -> HashableEntity | None:
        return self._parent_map.get((type(entity), entity.id))

    async def _compute_hash_for(  # type: ignore[override]
        self,
        entity: HashableEntity,
        *,
        exclude_ids: set[uuid.UUID] | None,
    ) -> str:
        self.compute_calls.append((type(entity), entity.id, exclude_ids))
        if self._hash_func is not None:
            result: str = self._hash_func(entity, exclude_ids)
            return result
        return _stable_hash_for(entity)  # type: ignore[arg-type]


# ── compute_raw_hash ──────────────────────────────────────────────


class TestComputeRawHash:
    def test_known_value_hello(self) -> None:
        """Acceptance #5: deterministic check on a known input."""
        assert compute_raw_hash(b"hello") == (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_deterministic_across_calls(self) -> None:
        assert compute_raw_hash(b"abc") == compute_raw_hash(b"abc")

    def test_differs_for_different_input(self) -> None:
        assert compute_raw_hash(b"abc") != compute_raw_hash(b"abd")

    def test_returns_64_char_lowercase_hex(self) -> None:
        digest = compute_raw_hash(b"hello")
        assert len(digest) == 64
        assert digest == digest.lower()
        assert all(c in "0123456789abcdef" for c in digest)


# ── compute_content_hash ──────────────────────────────────────────


class TestComputeContentHash:
    def test_order_independent_across_children(self) -> None:
        """Acceptance #6: re-ordering siblings does not invalidate parent."""
        assert compute_content_hash(b"x", ["a", "b"]) == compute_content_hash(
            b"x", ["b", "a"]
        )

    def test_local_content_change_changes_hash(self) -> None:
        assert compute_content_hash(b"x", []) != compute_content_hash(b"y", [])

    def test_separator_protects_against_concatenation_collision(self) -> None:
        # Without the \x00 separator, ['abc','def'] would collide with
        # ['abcd','ef'] because their concatenations both produce "abcdef".
        assert compute_content_hash(b"x", ["abc", "def"]) != compute_content_hash(
            b"x", ["abcd", "ef"]
        )

    def test_empty_children_hashes_local_only(self) -> None:
        from hashlib import sha256

        assert compute_content_hash(b"x", []) == sha256(b"x").hexdigest()

    def test_empty_local_with_children_hashes_only_children(self) -> None:
        # Used by Section / Node — local_content = b"".
        h = compute_content_hash(b"", ["a", "b"])
        assert len(h) == 64

    def test_returns_64_char_hex(self) -> None:
        assert len(compute_content_hash(b"x", ["a", "b"])) == 64


# ── invalidate_up — walker engine ─────────────────────────────────


class TestInvalidateUpWalksToRoot:
    async def test_walks_full_chain_and_writes_each_hash(self) -> None:
        a, b, c = A(), B(), C()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): c,
            (C, c.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        await svc.invalidate_up(a)

        assert a.content_hash == _stable_hash_for(a)
        assert b.content_hash == _stable_hash_for(b)
        assert c.content_hash == _stable_hash_for(c)
        session.flush.assert_awaited_once()

    async def test_single_flush_per_call(self) -> None:
        a = A()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        await svc.invalidate_up(a)

        session.flush.assert_awaited_once()


class TestInvalidateUpShortCircuit:
    async def test_stops_when_recomputed_hash_equals_stored(self) -> None:
        """Metadata-only change → segment hash unchanged → walk stops."""
        a = A()
        b = B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): None,
        }
        # Pre-set a's hash to the value the stub will recompute.
        a.content_hash = _stable_hash_for(a)

        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)
        await svc.invalidate_up(a)

        # b was never visited (short-circuit at a).
        assert b.content_hash is None
        # Only one compute call (for a).
        assert len(svc.compute_calls) == 1
        assert svc.compute_calls[0][0] is A


class TestInvalidateUpSoftDeletedHandling:
    async def test_skips_update_on_soft_deleted_but_walks_parent(self) -> None:
        """Trigger from 0.1 would block UPDATE on deleted rows; skip + continue."""
        a_deleted = A(deleted_at=datetime(2026, 1, 1, tzinfo=UTC))
        b = B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a_deleted.id): b,
            (B, b.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        await svc.invalidate_up(a_deleted)

        # a's hash NOT updated (would have been blocked by trigger).
        assert a_deleted.content_hash is None
        # b WAS updated — the parent-visit happens regardless.
        assert b.content_hash is not None
        # No compute call for the deleted entity itself.
        compute_types = [c[0] for c in svc.compute_calls]
        assert A not in compute_types
        assert B in compute_types


class TestInvalidateUpCycleSafety:
    async def test_visited_set_terminates_cycle(self) -> None:
        a, b = A(), B()
        # Cycle: A.parent = B, B.parent = A.
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): a,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        # Must terminate — no infinite loop.
        await svc.invalidate_up(a)

        # Both entities visited exactly once each.
        assert len(svc.compute_calls) == 2
        types_visited = {c[0] for c in svc.compute_calls}
        assert types_visited == {A, B}


class TestInvalidateUpDepthCap:
    async def test_long_chain_does_not_run_away(self) -> None:
        """Chain longer than _MAX_WALK_DEPTH terminates without crash."""
        # Chain length = cap + 15: comfortably above the cap so the
        # depth check is guaranteed to fire, but not so long that a
        # bug producing infinite recursion would be obscured.
        chain_length = _MAX_WALK_DEPTH + 15
        chain = [A() for _ in range(chain_length)]
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, chain[i].id): chain[i + 1] for i in range(len(chain) - 1)
        }
        parent_map[(A, chain[-1].id)] = None

        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        await svc.invalidate_up(chain[0])

        # The walker processes at most ``_MAX_WALK_DEPTH + 1`` entities
        # before the depth check returns; importantly, fewer than the
        # full chain — the cap fired.
        processed = sum(1 for e in chain if e.content_hash is not None)
        assert 0 < processed <= _MAX_WALK_DEPTH + 1
        assert processed < chain_length


# ── invalidate_subtree — bulk + dedup ─────────────────────────────


class TestInvalidateSubtreeShortCircuit:
    async def test_empty_ids_short_circuits_without_db_or_flush(self) -> None:
        """Acceptance #2 pitfall guard: empty IN (...) is invalid SQL."""
        session = AsyncMock()
        svc = ContentHashService(session)

        await svc.invalidate_subtree([])

        session.execute.assert_not_called()
        session.flush.assert_not_called()


class TestWalkUpDedup:
    """Direct test of the dedup primitive — the same shared ``visited`` set
    drives ``invalidate_subtree``'s "parent reached from N siblings is
    recomputed once" guarantee."""

    async def test_shared_visited_recomputes_parent_once(self) -> None:
        a1, a2 = A(), A()
        parent = B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a1.id): parent,
            (A, a2.id): parent,
            (B, parent.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        visited: set[tuple[type, uuid.UUID]] = set()
        await svc._walk_up(a1, visited, exclude_ids=None)
        await svc._walk_up(a2, visited, exclude_ids=None)

        # Parent compute fired exactly once across both walks.
        parent_computes = [c for c in svc.compute_calls if c[0] is B]
        assert len(parent_computes) == 1


# ── exclude_ids: both signatures ──────────────────────────────────


class TestExcludeIdsSignatures:
    async def test_walker_without_exclude_ids_updates_all_visited(self) -> None:
        a, b = A(), B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        await svc.invalidate_up(a)

        assert a.content_hash is not None
        assert b.content_hash is not None
        # Compute was called with exclude_ids=None.
        for call in svc.compute_calls:
            assert call[2] is None

    async def test_walker_with_exclude_ids_forwards_set_to_compute(self) -> None:
        a, b = A(), B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        excluded: set[uuid.UUID] = {uuid.uuid4(), uuid.uuid4()}
        visited: set[tuple[type, uuid.UUID]] = set()
        await svc._walk_up(a, visited, exclude_ids=excluded)

        # Same ``exclude_ids`` arrived at every compute call.
        for call in svc.compute_calls:
            assert call[2] == excluded

    async def test_excluded_entity_not_updated_but_parent_visited(self) -> None:
        """Cascade-time semantics: victims skipped, parent recomputed."""
        a, b = A(), B()
        parent_map: dict[tuple[type, uuid.UUID], FakeHashable | None] = {
            (A, a.id): b,
            (B, b.id): None,
        }
        session = AsyncMock()
        svc = StubContentHashService(session, parent_map=parent_map)

        visited: set[tuple[type, uuid.UUID]] = set()
        await svc._walk_up(a, visited, exclude_ids={a.id})

        # ``a`` is in exclude_ids → not updated.
        assert a.content_hash is None
        # ``b`` is the parent → recomputed (it remains active in the tree).
        assert b.content_hash is not None
        # No compute call on the excluded entity.
        compute_ids = [c[1] for c in svc.compute_calls]
        assert a.id not in compute_ids
        assert b.id in compute_ids
