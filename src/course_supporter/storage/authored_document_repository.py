"""Repository for AuthoredDocument CRUD and lifecycle management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from course_supporter.models.source import AssignmentType, MaterialRole
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.orm import AuthoredDocument, CourseNode


class AuthoredDocumentRepository:
    """Repository for material entry operations.

    Handles CRUD, pending receipt management, and hash invalidation.
    Not tenant-scoped — tenant isolation is ensured at the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        node_id: uuid.UUID,
        source_type: str,
        source_url: str,
        filename: str | None = None,
        material_role: str = "educational",
        task_type: AssignmentType | str | None = None,
        language: str | None = None,
        course_root_id: uuid.UUID | None = None,
        raw_hash: str | None = None,
        raw_size_bytes: int | None = None,
    ) -> AuthoredDocument:
        """Create a new material entry with auto-incremented order.

        Args:
            node_id: FK to the parent CourseNode.
            source_type: One of video, presentation, text, web.
            source_url: URL or storage path for the raw material.
            filename: Original filename (for uploads).
            material_role: Role: educational or methodological.
            task_type: Optional AssignmentType when this material represents
                a concrete task (test, short_task, task, project). NULL for
                regular materials.
            language: Optional ISO 639-1 language override. When None,
                the course default is used and STT falls back to
                auto-detection (which caches its result back here).
            course_root_id: Optional KD-delta denormalized root id. When
                omitted, the repository walks ``node_id``'s parent chain
                to derive it (defense-in-depth: walk is tenant-scoped to
                the parent's tenant per rule #12 — a malformed parent
                pointing at a different tenant terminates the walk
                early and raises). Callers that already have the root
                in scope (e.g. ingestion pipeline working from the
                course context) can pass it directly to skip the walk.
            raw_hash: Optional pre-computed SHA-256 hex digest of the
                authored bytes (Strategy A per KD-2.1-E). Set by the
                upload entry points (multipart ``create_document`` and
                presigned ``confirm_upload``) for file-backed uploads;
                ``None`` for URL-only materials where the bytes are
                fetched later by the ingestion worker. Once persisted
                the value is immutable (vision §3 KD9).
            raw_size_bytes: Optional byte count of the authored input
                paired with ``raw_hash`` per Strategy A original design
                (INVESTIGATION.md §6.5; sealed PHASE.md §"Коміт 8"
                omitted by abbreviation — D17 acknowledged deviation).
                Populated alongside ``raw_hash`` from the same buffer
                via ``len(upload_bytes)``. ``None`` for URL-only paths.

        Returns:
            The newly created AuthoredDocument.

        Raises:
            ValueError: If ``node_id`` does not exist; ``course_root_id``
                is not provided and the parent walk cannot resolve a
                root within the parent's tenant; or ``course_root_id``
                is provided but does not exist or belongs to a foreign
                tenant (rule #12 + KD-delta tenant scope).
        """
        if course_root_id is not None:
            # Defense-in-depth: caller-supplied course_root_id must
            # belong to the same tenant as node_id (rule #12 +
            # KD-delta tenant scope). Prior to hotfix-13 this branch
            # accepted the caller's value unchanged, which created a
            # cross-tenant data-leak vector via KD-delta scope-filtering
            # downstream.
            #
            # Race-condition disposition: race-against-delete excluded
            # by KD3 soft-delete contract (CourseNode never hard-deleted
            # at runtime); race-against-tenant-change excluded by Phase
            # 1 immutable tenant_id invariant. Pessimistic lock
            # (SELECT FOR UPDATE) deemed unnecessary.
            node = await self._session.get(CourseNode, node_id)
            if node is None:
                msg = f"CourseNode not found: {node_id}"
                raise ValueError(msg)
            root_node = await self._session.get(CourseNode, course_root_id)
            if root_node is None or root_node.tenant_id != node.tenant_id:
                msg = (
                    f"Invalid course_root_id {course_root_id}: not found "
                    f"or cross-tenant violation (node tenant {node.tenant_id})"
                )
                raise ValueError(msg)
        else:
            course_root_id = await self._resolve_course_root_id(node_id)
        next_order = await self._next_sibling_order(node_id)
        task_type_value: str | None
        if isinstance(task_type, AssignmentType):
            task_type_value = task_type.value
        else:
            task_type_value = task_type
        entry = AuthoredDocument(
            course_node_id=node_id,
            course_root_id=course_root_id,
            source_type=source_type,
            source_url=source_url,
            filename=filename,
            material_role=material_role,
            task_type=task_type_value,
            language=language,
            order=next_order,
            raw_hash=raw_hash,
            raw_size_bytes=raw_size_bytes,
        )
        self._session.add(entry)
        await self._session.flush()
        # Single-call materialization: ``invalidate_up`` walks from the
        # new entry up the parent chain, computing ``content_hash`` for
        # the entry itself and every ancestor. Replaces the legacy
        # ``_invalidate_node_chain`` call here per Phase 1.1 §6.7.1
        # variant (a): one call covers entity-level + parent chain in
        # a single walk, fixing the KD9 NULL-on-INSERT regression
        # (vision §3 KD9 line 580).
        await ContentHashService(self._session).invalidate_up(entry)
        return entry

    async def _resolve_course_root_id(self, node_id: uuid.UUID) -> uuid.UUID:
        """Compute the KD-delta root id for ``node_id`` via parent walk.

        Loads the parent ``CourseNode`` to extract its ``tenant_id``,
        then delegates to :meth:`CourseNodeRepository.get_root_for`
        with the tenant filter applied to both the base and recursive
        steps of the CTE. Tenant-scoped walking is defence-in-depth
        per rule #12: a malformed tree where ``node_id``'s parent
        chain crosses tenants returns ``None`` rather than silently
        resolving to a foreign tenant's root.

        Raises:
            ValueError: If ``node_id`` does not exist, or the
                tenant-scoped walk cannot reach a root (cross-tenant
                ``parent_id`` corruption).
        """
        from course_supporter.storage.course_node_repository import (
            CourseNodeRepository,
        )

        parent_node = await self._session.get(CourseNode, node_id)
        if parent_node is None:
            msg = f"CourseNode not found: {node_id}"
            raise ValueError(msg)
        node_repo = CourseNodeRepository(self._session)
        root = await node_repo.get_root_for(node_id, tenant_id=parent_node.tenant_id)
        if root is None:
            msg = (
                f"Cannot resolve course_root_id for {node_id}: parent walk "
                f"did not reach a tenant-{parent_node.tenant_id} root "
                f"(possible cross-tenant parent_id corruption)"
            )
            raise ValueError(msg)
        return root.id

    async def get_by_id(self, entry_id: uuid.UUID) -> AuthoredDocument | None:
        """Get an entry by primary key."""
        return await self._session.get(AuthoredDocument, entry_id)

    async def get_for_node(
        self, node_id: uuid.UUID, *, source_type: str | None = None
    ) -> list[AuthoredDocument]:
        """Get entries for a node, ordered by position.

        Args:
            node_id: FK to the parent CourseNode.
            source_type: Optional filter by source type.
        """
        stmt = (
            select(AuthoredDocument)
            .where(AuthoredDocument.course_node_id == node_id)
            .order_by(AuthoredDocument.order)
            # L3 (Рат.6): eager-load each row's in-flight Job so the
            # ``processing_phase`` derivation on the list response is
            # O(1)-in-queries (one batched IN-select), not a per-row lazy
            # load (MissingGreenlet under async). Flat, no ``load_only``.
            .options(selectinload(AuthoredDocument.pending_job))
        )
        if source_type is not None:
            stmt = stmt.where(AuthoredDocument.source_type == source_type)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_source_url(self, source_url: str) -> AuthoredDocument | None:
        """Find an entry by its source_url (exact match)."""
        stmt = select(AuthoredDocument).where(AuthoredDocument.source_url == source_url)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def find_by_raw_hash(self, raw_hash: str) -> AuthoredDocument | None:
        """Find the oldest active AuthoredDocument with the given ``raw_hash``.

        Used by ingestion to detect re-uploads of identical content
        (vision §3 KD9 + KD4). Filters out soft-deleted rows
        (``deleted_at IS NULL``); v0.20 intentionally allows the same
        ``raw_hash`` to appear on multiple active rows, so ``.first()``
        is the right shape rather than ``.scalar_one_or_none()``.
        Ordered by ``id`` (UUIDv7, time-ordered) so the result is
        deterministic across calls — duplicate-hit logging and tests
        do not flap when the index iteration order changes. ``id``
        carries a unique index, so the ORDER BY adds no meaningful
        cost. Callers needing tenant scoping or "all matches" should
        issue a custom query.
        """
        stmt = (
            select(AuthoredDocument)
            .where(AuthoredDocument.raw_hash == raw_hash)
            .where(AuthoredDocument.deleted_at.is_(None))
            .order_by(AuthoredDocument.id)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def set_pending(
        self,
        entry_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> AuthoredDocument:
        """Mark entry as pending ingestion.

        Sets job_id and pending_since, clears error_message.

        Args:
            entry_id: Entry to mark.
            job_id: FK to the Job performing ingestion.
            now: Override for current time (testing).

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        now = now or datetime.now(UTC)
        entry.job_id = job_id
        entry.pending_since = now
        entry.error_message = None
        await self._session.flush()
        return entry

    async def complete_processing(
        self,
        entry_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> AuthoredDocument:
        """Transition a document from PENDING to READY.

        Symmetric to :meth:`set_pending` (writes the receipt) and
        :meth:`fail_processing` (clears the receipt with an error).
        Clears the pending receipt and stamps ``processed_at``; the
        ``state`` derivation property (``orm.AuthoredDocument.state``)
        reads ``job_id IS NULL`` as READY.

        Args:
            entry_id: AuthoredDocument id to mark READY.
            now: Override for current time (testing).

        Returns:
            The updated AuthoredDocument.

        Raises:
            ValueError: If the entry is not found.
        """
        entry = await self._require(entry_id)
        entry.job_id = None
        entry.pending_since = None
        entry.error_message = None
        entry.processed_at = now or datetime.now(UTC)
        await self._session.flush()
        return entry

    async def fail_processing(
        self,
        entry_id: uuid.UUID,
        *,
        error_message: str,
        error_category: ErrorCategory | None = None,
    ) -> AuthoredDocument:
        """Mark entry as failed processing.

        Clears pending receipt and sets error_message (+ the optional
        structural error_category, F4 — write-site validated, stored as
        the enum ``.value`` string).

        Args:
            entry_id: Entry to update.
            error_message: Human-readable error description.
            error_category: Optional structural async-error code.

        Raises:
            ValueError: If entry not found, or when ``error_category``
                is not an ``ErrorCategory``.
        """
        entry = await self._require(entry_id)
        entry.job_id = None
        entry.pending_since = None
        entry.error_message = error_message
        if error_category is not None:
            if not isinstance(error_category, ErrorCategory):
                msg = f"error_category must be an ErrorCategory, got {error_category!r}"
                raise ValueError(msg)
            entry.error_category = error_category.value
        await self._session.flush()
        return entry

    async def store_safety_result(
        self,
        entry_id: uuid.UUID,
        *,
        safety_result: dict[str, object],
    ) -> AuthoredDocument:
        """Persist Stage 2 LLM safety verdict (pass or reject).

        Stores the serialized :class:`SafetyResult` JSON regardless
        of ``is_safe`` outcome. On rejection, ``arq_ingest_material``
        commits this row before raising ``SecurityRejectedError`` so
        that the verdict survives the subsequent rollback and the
        callback can read or audit it independently (Phase 2.1 C6
        per KD-2.1-P).

        Args:
            entry_id: AuthoredDocument id to update.
            safety_result: Serialized SafetyResult JSON shape (from
                ``SafetyResult.model_dump(mode="json")``).

        Returns:
            The updated AuthoredDocument.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.safety_result = safety_result
        await self._session.flush()
        return entry

    async def store_file_roles_proposal(
        self,
        entry_id: uuid.UUID,
        *,
        proposal: dict[str, object],
    ) -> AuthoredDocument:
        """Persist the DOCUMENT_PREPARATION file-role proposal (№21).

        Writes ONLY the ``proposal`` key of ``file_roles``. Any existing
        ``decision`` (the author's confirmation) is preserved untouched — a
        re-run of prep refreshes the proposal but never clobbers the decision
        (invariant I1 mirror; supports criterion 4 / decision 19: the author's
        markup survives a re-run). The whole dict is reassigned (not mutated in
        place)
        so SQLAlchemy detects the JSONB change.

        Args:
            entry_id: AuthoredDocument id to update.
            proposal: The proposal block ``{files, tree_digest, computed_at}``
                from :func:`course_supporter.ingestion.file_roles.build_role_proposal`.

        Returns:
            The updated AuthoredDocument.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.file_roles = {**(entry.file_roles or {}), "proposal": proposal}
        await self._session.flush()
        return entry

    async def set_slide_keys(
        self,
        entry_id: uuid.UUID,
        *,
        slide_keys: list[str],
    ) -> AuthoredDocument:
        """Persist the ordered per-slide WebP S3 keys (Phase 6 T3, KD17).

        Written on the ``arq_ingest_material`` seam after Pass 2b for
        presentation sources; ``arq_ingest_material`` flushes this inside the
        same business unit so the keys become durable together with the Pass
        2b segments (the existing commit). Overwrites in place on re-ingest.

        Args:
            entry_id: AuthoredDocument id to update.
            slide_keys: Ordered S3 keys (index = slide_number - 1).

        Returns:
            The updated AuthoredDocument.

        Raises:
            ValueError: If entry not found.
        """
        entry = await self._require(entry_id)
        entry.slide_keys = slide_keys
        await self._session.flush()
        return entry

    async def update_material_role(
        self,
        entry: AuthoredDocument,
        *,
        material_role: MaterialRole,
    ) -> AuthoredDocument:
        """Update the material_role field on an already-loaded entry.

        Args:
            entry: Entry ORM model to update.
            material_role: New role (MaterialRole.EDUCATIONAL or METHODOLOGICAL).
        """
        entry.material_role = material_role.value
        await self._session.flush()
        return entry

    async def update_task_type(
        self,
        entry: AuthoredDocument,
        *,
        task_type: AssignmentType | None,
    ) -> AuthoredDocument:
        """Update the task_type field on an already-loaded entry.

        Args:
            entry: Entry ORM model to update.
            task_type: New AssignmentType, or None to clear the task flag.
        """
        entry.task_type = task_type.value if task_type is not None else None
        await self._session.flush()
        return entry

    # ── Private helpers ──

    async def _invalidate_node_chain(self, node_id: uuid.UUID) -> None:
        """Recompute parent chain ``content_hash`` from node up to root."""
        node = await self._session.get(CourseNode, node_id)
        if node is not None:
            await ContentHashService(self._session).invalidate_up(node)

    async def _require(self, entry_id: uuid.UUID) -> AuthoredDocument:
        """Get entry or raise ValueError."""
        entry = await self.get_by_id(entry_id)
        if entry is None:
            msg = f"AuthoredDocument not found: {entry_id}"
            raise ValueError(msg)
        return entry

    async def _next_sibling_order(self, node_id: uuid.UUID) -> int:
        """Get next order value for entries under the given node."""
        stmt = select(func.coalesce(func.max(AuthoredDocument.order) + 1, 0)).where(
            AuthoredDocument.course_node_id == node_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
