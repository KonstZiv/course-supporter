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

            # Extension points
            await self._invalidate_fingerprints(session, material_id=material_id)

            await session.commit()

        log.info("ingestion_callback_success")

    async def on_failure(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        error_message: str,
    ) -> None:
        """Handle failed ingestion.

        Opens a fresh session to persist error state after rollback.

        Args:
            job_id: The Job tracking this ingestion.
            material_id: The material that failed.
            error_message: Human-readable error description.
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

            await session.commit()

        log.info("ingestion_callback_failure", error=error_message)

    # ------------------------------------------------------------------
    # Extension hooks
    # ------------------------------------------------------------------

    async def _invalidate_fingerprints(
        self,
        session: AsyncSession,
        *,
        material_id: uuid.UUID,
    ) -> None:
        """Invalidate Merkle fingerprints from material up to root."""
