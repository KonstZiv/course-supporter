"""Repository for NodeSummaryFinal lifecycle (vision §3 KD11).

The single shape that owns every write into ``node_summaries_final`` +
``node_summaries_final_previous_snapshots``. Orchestrator (channels 1 + 3)
and HTTP routes (channel 2 + approve + accept-raw + read paths in Phase
3.2.4 c2) MUST go through this class — direct SQL on either table is
forbidden by vision-side invariant 1 (TASK 3.2.4).

Three write channels (KD11 §1055-1071):

* **Channel 1** — automatic overwrite at Raw regeneration.
  :meth:`write_from_raw_with_snapshot` is invoked by the orchestrator
  after Pass 1 fixes ``source_content_hash`` for a node. First-time call
  INSERTs the Final row (no snapshot — there is no prior version);
  subsequent calls copy the live Final into the single-version snapshot
  (creating or overwriting that snapshot) and then bitwise-overwrite
  Final from Raw with ``approved_at = NULL`` (KD11 line 1060). Bitwise
  here means the shared field family ONLY: ``methodist_observations`` and
  ``compressed_summary`` from Raw are filtered out per vision.md:1059
  (Final never carries either column).

* **Channel 2** — author manual edit. :meth:`patch_editable` writes the
  hard-coded editable field family of KD11 §1043-1054. ``is_manual`` is
  intentionally NOT flipped: per ratified Phase 3.2.4 pre-flight (a),
  ``is_manual`` is a forward-compat marker for the empty-leaf author-
  create flow (no HTTP API in 3.2.4 creates such Finals), not a "this
  Final was author-edited" signal. The flag stays at its default
  ``false`` across every channel in 3.2.4.

* **Channel 3** — top-down ``enclosing_context`` refresh.
  :meth:`refresh_enclosing_context` is invoked by the orchestrator after
  Pass 2 materialises ``enclosing_context_source_hash``. Writes ONLY
  ``enclosing_context`` + ``enclosing_context_updated_at``; explicitly
  does NOT reset ``approved_at`` (KD11 line 1066 explicit exception —
  enclosing_context is not "content of the node" and must not
  invalidate the author's prior approval). Does NOT create a snapshot
  (snapshots track prior content versions; enclosing_context is not
  content per Final's own content_hash formula — see invariant 6).

Plus two read paths used by the Phase 3.2.4 c2 HTTP routes:
:meth:`get_by_course_node_id` (P6 GET + PATCH/approve/accept-raw lookup)
and :meth:`get_edit_view` (combined edit-view, KD11 §1086-1101).

Snapshot lifecycle (KD11 §1107-1111):

* Created on the FIRST channel-1 overwrite (the very first INSERT has
  no prior version → no snapshot).
* REPLACED in place on every subsequent channel-1 overwrite.
* HARD-DELETED on explicit approve / accept-raw (operator-ratified
  Phase 3.2.4: derived single-version data, soft-delete would leave a
  resurrect-ambiguity that vision-side does not need).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import (
    NodeSummaryFinal,
    NodeSummaryFinalPreviousSnapshot,
    NodeSummaryRaw,
)

_SHARED_FIELDS_FROM_RAW: tuple[str, ...] = (
    "title",
    "description",
    "learning_objectives",
    "knowledge",
    "skills",
    "success_criteria",
    "assessment_approach",
    "teaching_approach",
    "key_activities",
    "common_mistakes",
    "main_concepts",
    "secondary_concepts",
    "enclosing_context",
    "own_documents_count",
    "own_chars_count",
    "cumulative_documents_count",
    "cumulative_chars_count",
)
"""Fields copied bitwise from Raw to Final on channel 1.

Filters out ``methodist_observations`` and ``compressed_summary`` per
vision.md:1059 (Raw-only, never propagated to Final). Also excludes
the hash columns (``content_hash`` / ``enclosing_context_source_hash``
/ ``source_content_hash``) since Final's ``content_hash`` lives on its
own formula (KD9 — Final hash excludes ``enclosing_context``) and
``is_manual`` / ``manual_description`` / ``approved_at`` /
``enclosing_context_updated_at`` are Final-only state/timestamps.
"""

_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "learning_objectives",
        "knowledge",
        "skills",
        "success_criteria",
        "assessment_approach",
        "teaching_approach",
        "key_activities",
        "common_mistakes",
    }
)
"""Editable field family per KD11 §1043-1054 (Identity + Learning
outcomes + Assessment + Methodology). Concepts / enclosing_context /
meta intentionally excluded. ``patch_editable`` rejects keys outside
this set."""

_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "learning_objectives",
    "knowledge",
    "skills",
    "success_criteria",
    "assessment_approach",
    "teaching_approach",
    "key_activities",
    "common_mistakes",
    "main_concepts",
    "secondary_concepts",
    "enclosing_context",
    "is_manual",
    "manual_description",
    "own_documents_count",
    "own_chars_count",
    "cumulative_documents_count",
    "cumulative_chars_count",
    "content_hash",
    "approved_at",
    "enclosing_context_updated_at",
)
"""All content + state fields captured in the snapshot JSONB blob.

Captures the prior author-meaningful state of Final at the moment of
overwrite. Timestamps that describe the snapshot row itself (created_at,
replaced_at) live on the snapshot table, not in the JSONB blob.
"""


class NodeSummaryFinalRepository:
    """Single ownership point for ``node_summaries_final*`` writes.

    Not tenant-scoped at the repository level — Final inherits tenant
    isolation through ``course_node_id`` (UNIQUE 1:1 per KD11). Routes
    in Phase 3.2.4 c2 enforce tenant via the existing
    ``_require_node_for_tenant`` helper before calling repository
    methods.

    Repository methods do NOT commit. Callers (orchestrator,
    HTTP routes) own the transaction boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ─── Read paths ─────────────────────────────────

    async def get_by_course_node_id(
        self, course_node_id: uuid.UUID
    ) -> NodeSummaryFinal | None:
        """Fetch the active Final attached to a CourseNode (1:1)."""
        stmt = select(NodeSummaryFinal).where(
            NodeSummaryFinal.course_node_id == course_node_id,
            NodeSummaryFinal.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_snapshot_by_final_id(
        self, final_id: uuid.UUID
    ) -> NodeSummaryFinalPreviousSnapshot | None:
        """Fetch the single-version snapshot attached to a Final, if any."""
        stmt = select(NodeSummaryFinalPreviousSnapshot).where(
            NodeSummaryFinalPreviousSnapshot.node_summary_final_id == final_id,
            NodeSummaryFinalPreviousSnapshot.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_edit_view(
        self, course_node_id: uuid.UUID
    ) -> (
        tuple[NodeSummaryFinal, list[str], NodeSummaryFinalPreviousSnapshot | None]
        | None
    ):
        """Return ``(final, raw_observations, snapshot | None)`` for KD11 edit-view.

        Returns ``None`` when either Final or Raw is missing — the c2
        route maps that into the same generic 404 / 404 not_yet_generated
        contract that P6 GET uses.

        ``raw_observations`` is the API-side alias for the ORM column
        ``NodeSummaryRaw.methodist_observations`` (operator-ratified
        Phase 3.2.4 pre-flight (в) — rename only at the response shape,
        ORM name unchanged).
        """
        final = await self.get_by_course_node_id(course_node_id)
        if final is None:
            return None

        raw_stmt = select(NodeSummaryRaw.methodist_observations).where(
            NodeSummaryRaw.course_node_id == course_node_id,
            NodeSummaryRaw.deleted_at.is_(None),
        )
        raw_result = await self._session.execute(raw_stmt)
        observations = raw_result.scalar_one_or_none()
        if observations is None:
            return None

        snapshot = await self.get_snapshot_by_final_id(final.id)
        return (final, list(observations), snapshot)

    # ─── Channel 1 — automatic overwrite on Raw regeneration ──

    async def write_from_raw_with_snapshot(
        self, raw: NodeSummaryRaw
    ) -> NodeSummaryFinal:
        """Channel 1 — overwrite Final from Raw, snapshot prior version.

        KD11 §1055-1062 lifecycle:

        * Final absent (very first Raw landing for this node) → INSERT
          new Final as bitwise copy of Raw shared-field family,
          ``approved_at = NULL``. NO snapshot (no prior version exists).
        * Final present → snapshot the live state (create or overwrite
          the single 1:1 snapshot row), then bitwise-overwrite Final
          from Raw, ``approved_at = NULL``.

        ``is_manual`` is NEVER flipped (pre-flight question (a)
        ratified — flag is forward-compat for empty-leaf author flow,
        no API in 3.2.4 sets it). On INSERT it defers to the column
        server_default
        (``false``); on overwrite the prior value is preserved
        (orchestrator-driven channel 1 is the only caller in 3.2.4 and
        never touches that column directly).

        Returns the persisted Final ORM row (post-flush). Caller commits.
        """
        existing = await self.get_by_course_node_id(raw.course_node_id)

        if existing is None:
            final = NodeSummaryFinal(
                course_node_id=raw.course_node_id,
                **{name: getattr(raw, name) for name in _SHARED_FIELDS_FROM_RAW},
            )
            final.approved_at = None
            self._session.add(final)
            await self._session.flush()
            return final

        await self._snapshot_current_final(existing)

        for name in _SHARED_FIELDS_FROM_RAW:
            setattr(existing, name, getattr(raw, name))
        existing.approved_at = None
        await self._session.flush()
        return existing

    async def _snapshot_current_final(self, final: NodeSummaryFinal) -> None:
        """Capture the live Final state into the single-version snapshot.

        First snapshot for this Final → INSERT. Existing snapshot →
        UPDATE in place (1:1 invariant, single version per KD11 line
        1109). ``replaced_at`` is bumped on both paths so the consumer
        always sees the moment of the LAST overwrite.
        """
        existing_snapshot = await self.get_snapshot_by_final_id(final.id)
        payload = {name: getattr(final, name) for name in _SNAPSHOT_FIELDS}
        payload = self._jsonable_snapshot(payload)
        now = datetime.now(UTC)

        if existing_snapshot is None:
            snapshot = NodeSummaryFinalPreviousSnapshot(
                node_summary_final_id=final.id,
                snapshot=payload,
                replaced_at=now,
            )
            self._session.add(snapshot)
            await self._session.flush()
            return

        existing_snapshot.snapshot = payload
        existing_snapshot.replaced_at = now
        await self._session.flush()

    @staticmethod
    def _jsonable_snapshot(payload: dict[str, object]) -> dict[str, object]:
        """Render a snapshot payload into JSONB-safe primitives.

        Only ``datetime`` values need encoding (``approved_at``,
        ``enclosing_context_updated_at``). Everything else in
        :data:`_SNAPSHOT_FIELDS` is already a JSONB-native Python type
        (str / int / list / dict / bool / None).
        """
        out: dict[str, object] = {}
        for key, value in payload.items():
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            else:
                out[key] = value
        return out

    # ─── Channel 3 — top-down enclosing_context refresh ───────

    async def refresh_enclosing_context(
        self, raw: NodeSummaryRaw
    ) -> NodeSummaryFinal | None:
        """Channel 3 — point-update ``enclosing_context`` from Raw.

        KD11 §1064-1071 — sets Final.enclosing_context from Raw and
        bumps Final.enclosing_context_updated_at = now(). Explicitly
        does NOT reset Final.approved_at (KD11 line 1066 — enclosing_
        context is not "content of the node"). Does NOT create a
        snapshot (snapshots only track prior content versions).

        Returns ``None`` when Final does not exist for the node yet —
        legitimate state on the very first Pass 2 of a never-Pass-1ed
        ancestor; orchestrator's structural invariant (Pass 1 runs
        first, channel 1 creates Final) makes this branch unreachable
        in practice, but the repository stays defensive.
        """
        final = await self.get_by_course_node_id(raw.course_node_id)
        if final is None:
            return None

        final.enclosing_context = raw.enclosing_context
        final.enclosing_context_updated_at = datetime.now(UTC)
        await self._session.flush()
        return final

    # ─── Channel 2 — author manual edit ─────────────

    async def patch_editable(
        self, course_node_id: uuid.UUID, fields: dict[str, object]
    ) -> NodeSummaryFinal | None:
        """Channel 2 — partial author edit of the KD11 editable family.

        Accepts only keys in :data:`_EDITABLE_FIELDS` (Identity +
        Learning outcomes + Assessment + Methodology per KD11
        §1043-1054). Any other key raises ``ValueError`` — the c2
        HTTP route maps that into a 422; Pydantic request schema is
        the primary line of defence, the repository check is the
        deep gate.

        Does NOT touch ``approved_at`` (KD11: explicit approve is a
        separate action; an author edit does not auto-invalidate a
        prior approval) and does NOT touch ``is_manual`` (pre-flight
        question (a) ratified — forward-compat marker).

        Returns ``None`` when Final does not exist (route maps to 404).
        """
        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            msg = (
                f"NodeSummaryFinalRepository.patch_editable: non-editable "
                f"fields rejected per KD11 hard-coded editability table: "
                f"{sorted(unknown)}. Editable family: {sorted(_EDITABLE_FIELDS)}."
            )
            raise ValueError(msg)

        final = await self.get_by_course_node_id(course_node_id)
        if final is None:
            return None

        for name, value in fields.items():
            setattr(final, name, value)
        await self._session.flush()
        return final

    # ─── Approve / accept-raw ──────────────────────

    async def approve(self, course_node_id: uuid.UUID) -> NodeSummaryFinal | None:
        """Set ``approved_at = now()`` and hard-delete the snapshot.

        Snapshot hard-delete per operator-ratified Phase 3.2.4 pre-flight:
        once the author approves the current state, the prior version
        is irrelevant; soft-deleting would leave a resurrect-
        ambiguity that vision-side does not need.

        Returns ``None`` when Final does not exist (route maps to 404).
        """
        final = await self.get_by_course_node_id(course_node_id)
        if final is None:
            return None

        final.approved_at = datetime.now(UTC)
        await self._hard_delete_snapshot(final.id)
        await self._session.flush()
        return final

    async def accept_raw(self, raw: NodeSummaryRaw) -> NodeSummaryFinal | None:
        """Overwrite Final from Raw + approve + hard-delete snapshot.

        Implicit-approve flavour of channel 1 per KD11 §1079-1083: the
        author says "I take the current automatic version as canonical".
        Unlike channel 1, this path does NOT snapshot (we are not
        replacing an unapproved version with a newer automatic one — we
        are explicitly accepting the automatic version) and DOES set
        ``approved_at = now()``.

        Returns ``None`` when Final does not exist (route maps to 404).
        """
        final = await self.get_by_course_node_id(raw.course_node_id)
        if final is None:
            return None

        for name in _SHARED_FIELDS_FROM_RAW:
            setattr(final, name, getattr(raw, name))
        final.approved_at = datetime.now(UTC)
        await self._hard_delete_snapshot(final.id)
        await self._session.flush()
        return final

    async def _hard_delete_snapshot(self, final_id: uuid.UUID) -> None:
        """Drop the single-version snapshot row, if any."""
        stmt = delete(NodeSummaryFinalPreviousSnapshot).where(
            NodeSummaryFinalPreviousSnapshot.node_summary_final_id == final_id
        )
        await self._session.execute(stmt)
