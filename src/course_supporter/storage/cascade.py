"""Cascade soft-delete service (vision §3 KD3, KD12).

Engine for recursive soft-delete across declared FK relationships.
Concrete ``cascade_map`` definitions land per-entity in subsequent
phase tasks; this module ships only the engine, consumed either via
an explicit dict or via :func:`build_cascade_map` walking each model's
``__cascades_soft_delete_to__`` declaration.

Performance properties (relevant once concrete cascades are wired in
phases 1, 3, 4):

* **Breadth-first traversal.** The engine walks the cascade tree level
  by level rather than depth-first. At each level it groups newly-
  discovered entities by type and emits **one** batched ``SELECT ...
  WHERE fk_col IN (parent_ids)`` per ``(parent_type, child_type)``
  pair. This collapses what would otherwise be O(N) per-parent
  queries into O(L * T) where L = depth of the cascade and
  T = number of distinct (parent_type → child_type) pairs at each
  level. For a typical course tree (Tenant → CourseNode tree →
  AuthoredDocument → DocumentSummary → DocumentSegment) this is a
  small constant number of round-trips regardless of fan-out.
* **Cached FK resolution.** Mapper inspection runs once per
  ``(parent_cls, child_cls)`` pair across the process via
  :func:`functools.cache`; cascade traversals re-use the resolved
  columns without re-walking SQLAlchemy metadata.

Both optimisations preserve the original observable behaviour
(idempotent root, single pre-write hook with the full id list,
cycle-safe via a ``visited`` set, all-or-nothing write semantics).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol, cast

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper


class SoftDeletableEntity(Protocol):
    """Structural type for any model that participates in cascade delete.

    Concrete soft-deletable models (mixed with ``SoftDeleteMixin``) all
    expose these two attributes — ``id`` is declared on each model and
    ``deleted_at`` is contributed by the mixin.
    """

    id: uuid.UUID
    deleted_at: datetime | None


OnCancelJobs = Callable[[list[uuid.UUID]], Awaitable[None]]
"""Hook invoked once per cascade with all entity ids about to be deleted.

Concrete Job-cancellation logic lives outside this engine — wired by
the caller in task 0.3 and in per-entity tasks in later phases.
"""


OnInvalidateHashes = Callable[[list[uuid.UUID], set[uuid.UUID]], Awaitable[None]]
"""Hook invoked once per cascade with the full id list whose ``content_hash``
chains must be recomputed up to the root (vision §3 KD9 + KD12).

Single-shot pre-write semantics — invoked exactly once before any
``deleted_at`` write, so victims are still ``deleted_at IS NULL`` at
hook time.

The second argument is the **set of cascade victim ids** — the same
ids the hook just received as the first argument, repackaged as a
set for membership-test efficiency. Hook implementations forward
this set through to :meth:`ContentHashService.invalidate_subtree`'s
kw-only ``exclude_ids`` parameter (already present from task 0.2),
which treats the victims as "already gone" when recomputing
ancestor hashes. Without this signal the parent walk would issue
UPDATE statements on the victims themselves, and the soft-delete
protection trigger would trip the *next* mutation in the same
flush — by then the row carries the just-set ``deleted_at`` and
the trigger sees an UPDATE happen on a soft-deleted row. Passing
``exclude_ids = victim_ids`` short-circuits the UPDATE on victims
while still walking past them so their (surviving) parents
recompute as if the victims were already deleted.

Concrete implementations are wired in Phase 1 commit (k)/(l)/(m)
KD3-adoption handlers; the cascade-side signature extension here
is the Gap 3 fix.
"""


ScrubCallable = Callable[[Any], Awaitable[None]]
"""Hook applied to the *root* entity to perform per-type field scrubbing
(vision §3 KD3 / KD-β) atomically with the cascade ``deleted_at`` write.

Single-callable-per-cascade — by design the scrub targets only the root
entity, not cascade descendants. Per PHASE.md §1.3 audit, descendants of
the soft-deletable graph either carry no content fields (Tenant cascades
into APIKey/CourseNode/Student/HomeworkSubmission whose own scrub lists
are empty per §1.3) or are themselves the root of their own cascade
(AuthoredDocument is rooted at the ``delete_document`` route, where its
own ``scrub_authored_document`` callable applies). When a descendant
type later requires content scrub, the call site for that descendant's
own cascade is the right place to wire it.

Fires *after* the ``on_cancel_jobs`` and ``on_invalidate_hashes`` hooks
and *before* the ``deleted_at`` write — same flush boundary, so the
scrub mutation and the soft-delete mark land atomically.
"""


async def scrub_tenant_webhook_url(tenant: Any) -> None:
    """KD-β scrub: null out ``Tenant.webhook_url`` on soft-delete.

    ``Tenant`` is otherwise an identification-only model (per PHASE.md
    §1.3 audit) but carries an externally-configured webhook destination
    that must not survive soft-delete — vision-side decided KD-β with
    Option C ("null-out webhook_url"). Invoked by ``CascadeDeleteService``
    once on the root ``Tenant`` instance before the ``deleted_at`` write,
    so the NULL and the soft-delete mark land in the same flush.
    """
    tenant.webhook_url = None


async def scrub_authored_document(document: Any) -> None:
    """KD3 scrub: clear ``filename`` + ``source_url`` on AuthoredDocument soft-delete.

    Per PHASE.md §1.3 audit, ``AuthoredDocument`` carries two
    operationally-meaningful fields whose values must not survive
    soft-delete: ``filename`` (often the original-uploaded name —
    can leak naming conventions, course-internal taxonomy, or
    student-facing identifiers) and ``source_url`` (S3 object key
    or signed URL — leaks bucket layout and may carry signed-token
    residuals depending on which presigned variant was persisted
    at upload time). On soft-delete the file itself is being
    asynchronously hard-deleted via the ``s3_cleanup`` ARQ task
    (Phase 1 KD3 adoption); the URL has zero post-deletion
    operational value.

    ``filename`` is nullable in the schema and becomes ``NULL``;
    ``source_url`` is NOT NULL so the scrub sentinel is the empty
    string. The empty string is intentional rather than a marker
    like ``"[scrubbed]"``: it is the most database-idiomatic
    "no-value" representation for a NOT NULL text column, avoids
    allocating a magic literal that future readers must learn,
    and a query for ``source_url = ''`` is the canonical way to
    spot scrubbed rows in triage.

    Invoked by :class:`CascadeDeleteService` once on the root
    ``AuthoredDocument`` instance before the ``deleted_at`` write,
    so the scrub mutations and the soft-delete mark land in the
    same flush — observers cannot see a soft-deleted row that
    still carries the un-scrubbed values.
    """
    document.filename = None
    document.source_url = ""


def build_cascade_map(root: type) -> dict[type, list[type]]:
    """Build a cascade_map from ``__cascades_soft_delete_to__`` declarations.

    Walks the transitive closure starting from ``root``: every visited
    type contributes its ``__cascades_soft_delete_to__`` list as the
    map entry, and each direct descendant is then recursed into.

    Returns ``{type: [direct descendant types]}`` suitable for
    :meth:`CascadeDeleteService.soft_delete_with_cascade`.
    """
    cascade_map: dict[type, list[type]] = {}
    pending: list[type] = [root]
    while pending:
        cls = pending.pop()
        if cls in cascade_map:
            continue
        descendants: list[type] = list(getattr(cls, "__cascades_soft_delete_to__", []))
        cascade_map[cls] = descendants
        pending.extend(descendants)
    return cascade_map


@cache
def _resolve_cascade_columns(parent_cls: type, child_cls: type) -> tuple[Any, Any]:
    """Resolve the cascade query columns on ``child_cls``.

    Returns ``(fk_col, deleted_at_col)`` where ``fk_col`` is the column
    on ``child_cls`` that references ``parent_cls``'s mapped table and
    ``deleted_at_col`` is the soft-delete column on the same child
    table. Pure mapper inspection — does not touch the DB.

    Cached via ``functools.cache``: SQLAlchemy mapper metadata is
    fixed at import time, so each ``(parent_cls, child_cls)`` pair is
    inspected at most once per process. The cache key is the pair of
    classes themselves (both hashable).

    **Multi-FK disambiguation.** When ``child_cls`` has more than one
    FK to ``parent_cls``'s table (e.g. :class:`AuthoredDocument` carries
    both ``course_node_id`` parent FK and ``course_root_id`` denormalized
    root FK to ``course_nodes`` per KD-δ), the entity must declare
    ``__cascade_fk_from__: ClassVar[dict[str, str]]`` on the child class
    mapping ``parent_cls.__name__`` to the FK-column name to use for
    cascade resolution. String keys (rather than type objects) avoid
    forward-reference issues at module-load time and match SQLAlchemy's
    own ``relationship("ClassName", ...)`` convention. Without the
    declaration the call raises ``ValueError`` with a remediation hint
    naming the exact entry to add.

    Raises:
        ValueError: when ``child_cls`` has no foreign key to
            ``parent_cls`` (cascade misconfigured), or when it has
            more than one and no ``__cascade_fk_from__`` entry is
            declared (the message names the entry to add), or when
            ``__cascade_fk_from__`` declares a column name that does
            not match any of the FK columns found.
    """
    parent_mapper = cast(Mapper[Any], inspect(parent_cls))
    child_mapper = cast(Mapper[Any], inspect(child_cls))
    parent_table = parent_mapper.local_table

    fk_cols = [
        col
        for col in child_mapper.local_table.columns
        for fk in col.foreign_keys
        if fk.column.table is parent_table
    ]
    if not fk_cols:
        raise ValueError(
            f"No foreign key from {child_cls.__name__} to {parent_cls.__name__}"
        )
    if len(fk_cols) > 1:
        disambig = getattr(child_cls, "__cascade_fk_from__", {})
        parent_name = parent_cls.__name__
        if parent_name in disambig:
            target_name = disambig[parent_name]
            matching = [c for c in fk_cols if c.name == target_name]
            if not matching:
                raise ValueError(
                    f"__cascade_fk_from__[{parent_name!r}] = {target_name!r} "
                    f"on {child_cls.__name__} does not match any FK column. "
                    f"Available FKs to {parent_name}: "
                    f"{[c.name for c in fk_cols]}."
                )
            fk_cols = matching
        else:
            raise ValueError(
                f"Ambiguous foreign keys from {child_cls.__name__} "
                f"to {parent_cls.__name__}: {[c.name for c in fk_cols]}. "
                f'Add {{"{parent_cls.__name__}": "<fk_col_name>"}} entry '
                f"to {child_cls.__name__}.__cascade_fk_from__."
            )

    fk_col = fk_cols[0]
    deleted_at_col = child_mapper.local_table.c.deleted_at
    return fk_col, deleted_at_col


class CascadeDeleteService:
    """Recursive soft-delete engine (vision §3 KD3, KD12).

    Walks the ``cascade_map`` breadth-first from the root entity,
    collecting every reachable active descendant via batched queries,
    then writes ``deleted_at`` on all of them in the same DB session.
    The caller controls the transaction boundary — ``commit`` is not
    invoked here, so a failure mid-traversal rolls back the whole
    cascade with the surrounding transaction.

    See module docstring for the BFS + caching rationale. Concrete
    cascade maps land per-entity in subsequent phase tasks;
    :func:`build_cascade_map` constructs the map at runtime from each
    model's ``__cascades_soft_delete_to__`` declaration.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def soft_delete_with_cascade(
        self,
        entity: SoftDeletableEntity,
        cascade_map: dict[type, list[type]],
        *,
        on_cancel_jobs: OnCancelJobs | None = None,
        on_invalidate_hashes: OnInvalidateHashes | None = None,
        scrub_callable: ScrubCallable | None = None,
        now: datetime | None = None,
    ) -> None:
        """Soft-delete ``entity`` and every active descendant.

        Idempotent: returns immediately if ``entity`` is already
        soft-deleted (no UPDATE, no hook call, no scrub). Cycle-safe
        via a ``visited`` set keyed by ``(type, id)`` so cascade_map
        cycles cannot trigger infinite recursion.

        Pre-write phases are invoked exactly once before any
        ``deleted_at`` write, in declared parameter order:
        ``on_cancel_jobs`` first (stop in-flight work), then
        ``on_invalidate_hashes`` (recompute upstream Merkle hashes per
        vision §3 KD9 + KD12), then ``scrub_callable`` on the root
        entity (vision §3 KD3 / KD-β field scrubbing — applies only
        to the root, see :data:`ScrubCallable` for the rationale on
        single-callable-per-cascade). A failure in any of the three
        aborts the cascade cleanly: no rows are mutated and no flush
        happens.

        Scrub mutation and ``deleted_at`` write share the single
        terminal flush so the scrub-NULL and the soft-delete mark land
        atomically — observers cannot see a soft-deleted row that
        still carries the un-scrubbed value.

        ``now`` overrides the timestamp applied to all rows
        (defaults to ``datetime.now(UTC)``); useful for deterministic
        tests.
        """
        if entity.deleted_at is not None:
            return

        ts = now if now is not None else datetime.now(UTC)
        to_delete = await self._collect_all(entity, cascade_map)
        ids = [e.id for e in to_delete]

        if on_cancel_jobs is not None:
            await on_cancel_jobs(ids)
        if on_invalidate_hashes is not None:
            # Gap 3: pass the victim id-set as ``exclude_ids`` so the
            # parent-hash walk treats victims as "already gone" and
            # avoids issuing UPDATE on a row that is about to flip to
            # ``deleted_at IS NOT NULL`` in the same flush. See the
            # ``OnInvalidateHashes`` docstring for the trigger-trip
            # rationale.
            await on_invalidate_hashes(ids, set(ids))
        if scrub_callable is not None:
            await scrub_callable(entity)

        for row in to_delete:
            row.deleted_at = ts

        await self._session.flush()

    async def _collect_all(
        self,
        root: SoftDeletableEntity,
        cascade_map: dict[type, list[type]],
    ) -> list[SoftDeletableEntity]:
        """BFS across cascade levels with batched fetch per pair.

        At each level, newly-discovered entities are grouped by their
        concrete type. For every ``(parent_type, child_type)`` pair
        present in ``cascade_map``, a single ``SELECT ... WHERE fk IN
        (parent_ids) AND deleted_at IS NULL`` query collects the next
        level's children — independent of how many parents are at this
        level. The ``visited`` set deduplicates entities that are
        reachable through multiple paths (DAG cascades) and terminates
        cycles.
        """
        visited: set[tuple[type, uuid.UUID]] = set()
        to_delete: list[SoftDeletableEntity] = []
        frontier: list[SoftDeletableEntity] = [root]

        while frontier:
            current: list[SoftDeletableEntity] = []
            for entity in frontier:
                cls = type(entity)
                key = (cls, entity.id)
                if key in visited:
                    continue
                visited.add(key)
                if entity.deleted_at is not None:
                    continue
                to_delete.append(entity)
                current.append(entity)

            by_parent_type: dict[type, list[SoftDeletableEntity]] = defaultdict(list)
            for entity in current:
                by_parent_type[type(entity)].append(entity)

            next_frontier: list[SoftDeletableEntity] = []
            for parent_cls, parents in by_parent_type.items():
                child_classes = cascade_map.get(parent_cls, [])
                if not child_classes:
                    continue
                parent_ids = [p.id for p in parents]
                for child_cls in child_classes:
                    children = await self._fetch_active_children_batch(
                        parent_cls, parent_ids, child_cls
                    )
                    next_frontier.extend(children)

            frontier = next_frontier

        return to_delete

    async def _fetch_active_children_batch(
        self,
        parent_cls: type,
        parent_ids: list[uuid.UUID],
        child_cls: type,
    ) -> list[SoftDeletableEntity]:
        """Batched fetch of active ``child_cls`` rows whose FK ∈ ``parent_ids``.

        Issues a single ``SELECT ... WHERE fk_col IN (parent_ids) AND
        deleted_at IS NULL`` query. Returns ``[]`` immediately on an
        empty ``parent_ids`` (avoids constructing an invalid empty-IN
        SQL). FK resolution is delegated to the cached
        :func:`_resolve_cascade_columns`, so the per-call cost is just
        query construction + execution.
        """
        if not parent_ids:
            return []

        fk_col, deleted_at_col = _resolve_cascade_columns(parent_cls, child_cls)
        stmt: Any = (
            select(child_cls)
            .where(fk_col.in_(parent_ids))
            .where(deleted_at_col.is_(None))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
