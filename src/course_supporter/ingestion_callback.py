"""Post-ingestion callback: update Job, Material, and future extension hooks.

After an ingestion job completes (success or failure), this service:
1. Updates SourceMaterial status and content.
2. Updates Job status (complete/failed).
3. [Future — Epic 3] Invalidates Merkle fingerprints up the tree.
4. [Future — Epic 5] Triggers revalidation of blocked SlideVideoMappings.

The two-session pattern is encapsulated here: the caller provides a
session_factory, and this service handles rollback + error-session
internally for failure paths.

.. note::
    **S2-014 migration note**: When ``MaterialEntry`` replaces
    ``SourceMaterial`` (Epic 2, S2-014), update this service to use
    ``MaterialEntryRepository`` instead of ``SourceMaterialRepository``.
    All tests must be re-verified for the new model's field names
    (``processed_content`` vs ``content_snapshot``, ``pending_job_id``
    clearing, ``processed_hash`` / ``processed_at`` setting).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.repositories import SourceMaterialRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class IngestionCallback:
    """Handle post-ingestion updates for Job and Material records.

    Encapsulates the two-session pattern: success path uses the
    provided session, failure path opens a fresh session to persist
    error state after rollback.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def on_success(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        content_json: str,
        is_new_model: bool = False,
    ) -> None:
        """Handle successful ingestion completion.

        When ``is_new_model`` is True, uses ``MaterialEntryRepository``
        (``complete_processing``). Otherwise falls back to legacy
        ``SourceMaterialRepository`` (``update_status → done``).

        Args:
            job_id: The Job tracking this ingestion.
            material_id: The material that was processed.
            content_json: Serialized SourceDocument JSON.
            is_new_model: True when the material is a MaterialEntry.
        """
        import hashlib

        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            job_repo = JobRepository(session)

            if is_new_model:
                from course_supporter.storage.material_entry_repository import (
                    MaterialEntryRepository,
                )

                entry_repo = MaterialEntryRepository(session)
                processed_hash = hashlib.sha256(content_json.encode()).hexdigest()
                await entry_repo.complete_processing(
                    material_id,
                    processed_content=content_json,
                    processed_hash=processed_hash,
                )
            else:
                mat_repo = SourceMaterialRepository(session)
                await mat_repo.update_status(
                    material_id, "done", content_snapshot=content_json
                )

            await job_repo.update_status(
                job_id, "complete", result_material_id=material_id
            )

            # Extension points
            await self._invalidate_fingerprints(session, material_id=material_id)
            await self._revalidate_blocked_mappings(session, material_id=material_id)

            await session.commit()

        log.info("ingestion_callback_success")

    async def on_failure(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        error_message: str,
        is_new_model: bool = False,
    ) -> None:
        """Handle failed ingestion.

        Opens a fresh session (the caller's session is expected to have
        been rolled back already) to persist error state.

        When ``is_new_model`` is True, uses ``MaterialEntryRepository``
        (``fail_processing``). Otherwise falls back to legacy
        ``SourceMaterialRepository`` (``update_status → error``).

        Args:
            job_id: The Job tracking this ingestion.
            material_id: The material that failed.
            error_message: Human-readable error description.
            is_new_model: True when the material is a MaterialEntry.
        """
        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            job_repo = JobRepository(session)

            await job_repo.update_status(job_id, "failed", error_message=error_message)

            if is_new_model:
                from course_supporter.storage.material_entry_repository import (
                    MaterialEntryRepository,
                )

                entry_repo = MaterialEntryRepository(session)
                await entry_repo.fail_processing(
                    material_id, error_message=error_message
                )
            else:
                mat_repo = SourceMaterialRepository(session)
                await mat_repo.update_status(
                    material_id, "error", error_message=error_message
                )

            # Extension point: update blocking_factors on mappings
            # that reference this material (Epic 5, S2-042).
            await self._revalidate_blocked_mappings(session, material_id=material_id)

            await session.commit()

        log.info("ingestion_callback_failure", error=error_message)

    # ------------------------------------------------------------------
    # Extension hooks — no-op stubs for future epics
    # ------------------------------------------------------------------

    async def _invalidate_fingerprints(
        self,
        session: AsyncSession,
        *,
        material_id: uuid.UUID,
    ) -> None:
        """Invalidate Merkle fingerprints from material up to course root.

        **Current state**: no-op stub.

        **Epic 3 (S2-027) implementation plan**:
        1. Load the MaterialEntry (or its parent MaterialNode).
        2. Set ``content_fingerprint = NULL`` on the entry.
        3. Walk up the tree via ``parent_id``, setting
           ``node_fingerprint = NULL`` on each ancestor node.
        4. Set course-level fingerprint to NULL.

        All changes are flushed within the provided session
        (caller commits).
        """

    async def _revalidate_blocked_mappings(
        self,
        session: AsyncSession,
        *,
        material_id: uuid.UUID,
    ) -> None:
        """Revalidate SlideVideoMappings blocked by this material."""
        from course_supporter.storage.mapping_validation import (
            MappingValidationService,
        )

        validator = MappingValidationService(session)
        count = await validator.revalidate_blocked(material_id)
        if count > 0:
            structlog.get_logger().bind(
                material_id=str(material_id),
            ).info("revalidated_blocked_mappings", count=count)
