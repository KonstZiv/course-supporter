"""Post-ingestion callback: update Job and AuthoredDocument records.

After an ingestion job completes (success or failure), this service:
1. Updates AuthoredDocument processing state.
2. Updates Job status (complete/failed).
3. Invalidates Merkle fingerprints up the tree.

The two-session pattern is encapsulated here: the caller provides a
session_factory, and this service handles rollback + error-session
internally for failure paths.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from course_supporter.storage.job_repository import JobRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.security.exceptions import ErrorCategory

logger = structlog.get_logger()


class IngestionCallback:
    """Handle post-ingestion updates for Job and AuthoredDocument records.

    Encapsulates the two-session pattern: success path uses the
    provided session, failure path opens a fresh session to persist
    error state after rollback.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def on_success(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        content_json: str,
    ) -> None:
        """Handle successful ingestion completion.

        Uses ``AuthoredDocumentRepository`` (``complete_processing``).

        Args:
            job_id: The Job tracking this ingestion.
            material_id: The material that was processed.
            content_json: Serialized SourceDocument JSON.
        """
        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            job_repo = JobRepository(session)

            from course_supporter.storage.authored_document_repository import (
                AuthoredDocumentRepository,
            )

            entry_repo = AuthoredDocumentRepository(session)
            await entry_repo.complete_processing(material_id)

            await job_repo.update_status(job_id, "complete")

            await session.commit()

        log.info("ingestion_callback_success")

    async def on_failure(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        error_message: str,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """Handle failed ingestion.

        Opens a fresh session to persist error state after rollback.
        When ``error_category == ErrorCategory.STAGE2_REJECTED``
        (Phase 2.1 C6 per KD-2.1-P) the failed AuthoredDocument is
        additionally soft-deleted via
        :meth:`CascadeDeleteService.soft_delete_with_cascade` in this
        same fresh session, so the operator-facing tree never displays
        a rejected document. Infrastructure failures and Stage 1
        synchronous rejections fall through the default branch — they
        only mark ``fail_processing`` and leave the row intact.

        Args:
            job_id: The Job tracking this ingestion.
            material_id: The material that failed.
            error_message: Human-readable error description.
            error_category: Optional :class:`ErrorCategory`. Currently
                only ``STAGE2_REJECTED`` triggers an additional
                cascade soft-delete; all other values (and ``None``)
                preserve the legacy ``fail_processing``-only flow.
        """
        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            job_repo = JobRepository(session)

            await job_repo.update_status(job_id, "failed", error_message=error_message)
            cascaded = await job_repo.propagate_failure(job_id)
            if cascaded:
                log.info("cascading_failure_propagated", failed_count=len(cascaded))

            from course_supporter.storage.authored_document_repository import (
                AuthoredDocumentRepository,
            )

            entry_repo = AuthoredDocumentRepository(session)
            await entry_repo.fail_processing(material_id, error_message=error_message)

            from course_supporter.security.exceptions import ErrorCategory

            if error_category == ErrorCategory.STAGE2_REJECTED:
                await self._soft_delete_rejected_document(
                    session, material_id=material_id, log=log
                )

            await session.commit()

        log.info(
            "ingestion_callback_failure",
            error=error_message,
            error_category=error_category.value if error_category else None,
        )

    async def _soft_delete_rejected_document(
        self,
        session: AsyncSession,
        *,
        material_id: uuid.UUID,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Cascade soft-delete an AuthoredDocument rejected by Stage 2.

        Pattern mirrors :func:`api.routes.documents.soft_delete_document`
        (cascade engine ordering invariant cancel → invalidate → scrub
        → write deleted_at → flush). Logs and swallows soft-delete
        failures so callback completion still records the failed-job
        + fail_processing state — Stage 2 reject persistence is the
        observable contract; cascade is a best-effort cleanup.

        Args:
            session: Active callback session (commit happens upstream).
            material_id: AuthoredDocument id to soft-delete.
            log: Bound logger from caller.
        """
        from course_supporter.jobs.cancellation_service import (
            JobCancellationService,
        )
        from course_supporter.storage.authored_document_repository import (
            AuthoredDocumentRepository,
        )
        from course_supporter.storage.cascade import (
            CascadeDeleteService,
            build_cascade_map,
        )
        from course_supporter.storage.content_hash import ContentHashService
        from course_supporter.storage.orm import AuthoredDocument

        entry_repo = AuthoredDocumentRepository(session)
        document = await entry_repo.get_by_id(material_id)
        if document is None:
            log.warning(
                "stage2_reject_cascade_skipped_missing",
                material_id=str(material_id),
            )
            return

        course_node_id = document.course_node_id
        cascade_service = CascadeDeleteService(session)
        cascade_map = build_cascade_map(AuthoredDocument)
        content_hash_service = ContentHashService(session)
        job_cancellation_service = JobCancellationService(session)

        async def invalidate_hook(
            ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
        ) -> None:
            await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

        async def cancel_hook(victim_ids: list[uuid.UUID]) -> None:
            augmented = [*victim_ids, course_node_id]
            await job_cancellation_service.cancel_jobs_for_entities(augmented)

        await cascade_service.soft_delete_with_cascade(
            document,
            cascade_map,
            on_cancel_jobs=cancel_hook,
            on_invalidate_hashes=invalidate_hook,
        )
        log.info("stage2_reject_cascade_complete", material_id=str(material_id))
