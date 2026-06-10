"""Second axis of methodist Merkle: enclosing-context staleness (Phase 3.2.1).

vision §3 KD10 lines 1011-1019: top-down memoization key for
``NodeSummaryRaw.enclosing_context_source_hash`` is the hash of the
**parent**'s ``enclosing_context``. Own ``content_hash`` is NOT in the
key — content (bottom-up) and context (top-down) are two independent
axes; if the own content shifts, the bottom-up service regenerates the
node (first axis), the second axis does NOT duplicate that work.

This module ships **per-node primitives only**: ``compute_source_hash_for``,
``is_stale``, ``update_source_hash``. The downward walk (root→leaves),
per-run / per-node statuses in ``Job.stage_progress``, and the LLM hook
between skip and refresh belong to the 3.2.2 orchestrator. KD10 commit-
per-node + run-as-first-class entity require that traversal lives in the
orchestrator, not here.

Symmetry note vs ``ContentHashService`` (storage/content_hash.py):
bottom-up walks a single column (one parent per node — linear), while
top-down is fan-out (a CourseNode has many children). The shapes are
deliberately not mirrored — only the **pure helpers**
(``compute_content_hash``, ``_encode_local_fields``) are reused; the
walker shape is owned by the orchestrator one level up.

**Bearing invariant (vision.md:1019 — closed by test).** Memoization of
the bottom-up axis keeps the canonical ``part1`` of NodeSummaryRaw byte-
stable while ``CourseNode.content_hash`` is stable. Without that cache,
LLM non-determinism would drift the generated ``enclosing_context`` from
re-run to re-run, child source_hashes would never match stored values,
and the second-axis short-circuit would degrade to "always recompute".
The second axis is correct ONLY because the first axis cache holds
``part1`` stable. ``test_enclosing_context_hash.py`` pins this with a
synthetic test that does not depend on real LLM generation.

**Root semantics (Q-B ratify, Phase 3.2.1).** For a root CourseNode
(``parent_id IS NULL``), ``compute_source_hash_for`` returns ``None``.
This is the asymmetric counterpart to the empty-hash convention on the
first axis (``EMPTY_NODE_CONTENT_HASH``): on the first axis an empty
node has a defined hash because the formula has something to compute
(``compute_content_hash(b"", [])``); on the second axis a root has no
parent, so there is nothing to hash — the key is **undefined by
construction**, not "empty". This deliberately does NOT violate the
Phase 3.1 Q-G "defined hash, never NULL" rule — that rule was about
``content_hash``, where every node participates; ``source_hash`` of a
root is absent by definition. Column nullability on
``NodeSummaryRaw.enclosing_context_source_hash`` (Phase 3.1) already
accommodates this.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Pure helper reuse per Q-A subpoint: import the private helper directly
# from the sibling module rather than promoting it to public API for a
# single new consumer in the same ``storage/`` package. If a third
# consumer arrives, the helper can be lifted to a shared module —
# premature now.
from course_supporter.storage.content_hash import (
    _encode_local_fields,
    compute_content_hash,
)
from course_supporter.storage.orm import CourseNode, NodeSummaryRaw


class EnclosingContextHashable(Protocol):
    """Structural type for participants in the second hash axis.

    ``NodeSummaryRaw`` is the sole concrete participant today (Phase
    3.1 ratified ``enclosing_context_source_hash`` lives on Raw only —
    not on Final, not on PreviousSnapshot). The Protocol shape mirrors
    ``HashableEntity`` in ``content_hash.py`` for single-purpose
    separation between the two axes: each axis declares its own
    contract, and a future contributor to either side does not have
    to thread fields through the other.

    Fields:

    - ``id`` / ``deleted_at`` — same lifecycle invariants as the first
      axis (skip soft-deleted entities; cycle-safe by id).
    - ``course_node_id`` — parent resolution starts here (load the
      ``CourseNode``, follow its ``parent_id`` to the parent ``CourseNode``,
      load the parent ``NodeSummaryRaw`` by its ``course_node_id``).
    - ``enclosing_context`` — read from the PARENT to compute the
      current node's key; never read from self for this axis.
    - ``enclosing_context_source_hash`` — written to self after a
      successful (re)compute; compared against the freshly-computed
      value for staleness.
    """

    id: uuid.UUID
    deleted_at: datetime | None
    course_node_id: uuid.UUID
    enclosing_context: str | None
    enclosing_context_source_hash: str | None


class EnclosingContextHashService:
    """Per-node primitives for the second axis (vision §3 KD10).

    Provides three async operations:

    - :meth:`compute_source_hash_for` — derive the current canonical
      key from the parent's ``enclosing_context``; ``None`` on root.
    - :meth:`is_stale` — compare stored vs currently-computed.
    - :meth:`update_source_hash` — materialise a new value on self.

    The service does NOT walk the tree. It does NOT decide when to
    invoke ``compute`` or ``update``. It does NOT enqueue jobs, hold
    per-run state, or coordinate LLM calls. All of that lives one
    level up in the 3.2.2 orchestrator — see KD10 commit-per-node +
    run-as-first-class entity for why traversal must own the loop.

    Stateless aside from the bound session; callers construct one
    inline per orchestrator step, no singleton.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_source_hash_for(
        self, node: EnclosingContextHashable
    ) -> str | None:
        """Return the canonical second-axis key for ``node``.

        Returns ``None`` when ``node`` is on a root ``CourseNode``
        (``parent_id IS NULL`` — top-down skips the root per vision
        §3 KD10 line 1001; the key is undefined by construction).
        Otherwise returns
        ``compute_content_hash(_encode_local_fields({"enclosing_context":
        parent.enclosing_context or ""}), [])`` over the PARENT
        ``NodeSummaryRaw`` resolved via the CourseNode tree.

        ``None``-coalesce to ``""`` on ``parent.enclosing_context``
        mirrors the convention used by sibling content_hash formulas
        (see ``_node_summary_raw_payload`` in ``content_hash.py``): an
        absent ``enclosing_context`` (parent not yet generated by
        3.2.3) hashes the same as an explicit empty string, so
        children get a deterministic "context-absent" source_hash
        until the parent receives its enclosing_context. Once the
        parent's value flips from ``None`` to a non-empty string,
        every child's computed source_hash flips, and orchestrator
        sees them as stale.
        """
        parent_raw = await self._fetch_parent_raw(node)
        if parent_raw is None:
            return None
        return compute_content_hash(
            _encode_local_fields(
                {"enclosing_context": parent_raw.enclosing_context or ""}
            ),
            [],
        )

    async def is_stale(self, node: EnclosingContextHashable) -> bool:
        """Return ``True`` when stored differs from currently-computed.

        Returns ``False`` when both the stored value and the freshly-
        computed value are ``None`` (root case, or a non-root child
        whose parent has no enclosing_context yet — stable absence).
        """
        raise NotImplementedError("Commit 3 of task 3.2.1")

    async def update_source_hash(
        self,
        node: EnclosingContextHashable,
        new_hash: str | None,
    ) -> None:
        """Set ``node.enclosing_context_source_hash`` and flush.

        ``new_hash=None`` is the legitimate root case; the column is
        nullable (Phase 3.1 schema). Caller is responsible for the
        surrounding transaction boundary; this method only flushes.
        """
        raise NotImplementedError("Commit 3 of task 3.2.1")

    async def _fetch_parent_raw(
        self, node: EnclosingContextHashable
    ) -> NodeSummaryRaw | None:
        """Resolve the parent ``NodeSummaryRaw`` via the CourseNode tree.

        Two-hop lookup:

        1. Load the current node's ``CourseNode`` (1:1 via
           ``course_node_id``).
        2. If ``course_node.parent_id IS NULL`` — the current node
           is on a root; return ``None``.
        3. Otherwise load the ``NodeSummaryRaw`` with
           ``course_node_id == parent_id`` (1:1 per "є вузол, є Raw").

        Returns ``None`` for the root case OR for the invariant-
        violation case "parent CourseNode exists but its NodeSummaryRaw
        does not yet exist". Caller (orchestrator) is expected to walk
        root→leaves, so a missing parent Raw means the orchestrator
        out-of-order'd — service does not raise; it returns ``None``
        and lets the downstream policy decide.
        """
        course_node = await self._session.get(CourseNode, node.course_node_id)
        if course_node is None or course_node.parent_id is None:
            return None
        parent_id = course_node.parent_id
        stmt = select(NodeSummaryRaw).where(
            NodeSummaryRaw.course_node_id == parent_id,
            NodeSummaryRaw.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
