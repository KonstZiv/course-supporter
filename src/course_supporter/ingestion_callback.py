"""Post-ingestion callback: update Job and MaterialEntry records.

After an ingestion job completes (success or failure), this service:
1. Updates MaterialEntry processing state.
2. Updates Job status (complete/failed).
3. Invalidates Merkle fingerprints up the tree.
4. Triggers revalidation of blocked SlideVideoMappings.

The two-session pattern is encapsulated here: the caller provides a
session_factory, and this service handles rollback + error-session
internally for failure paths.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

import structlog

from course_supporter.storage.job_repository import JobRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.llm.router import ModelRouter

logger = structlog.get_logger()


class IngestionCallback:
    """Handle post-ingestion updates for Job and MaterialEntry records.

    Encapsulates the two-session pattern: success path uses the
    provided session, failure path opens a fresh session to persist
    error state after rollback.

    When ``router`` is provided, generates a MaterialOutline after
    successful ingestion. Outline failures are logged but do not
    break the ingestion — ArchitectAgent falls back to raw content.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        router: ModelRouter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._router = router

    async def on_success(
        self,
        *,
        job_id: uuid.UUID,
        material_id: uuid.UUID,
        content_json: str,
    ) -> None:
        """Handle successful ingestion completion.

        Uses ``MaterialEntryRepository`` (``complete_processing``).

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

            await self._generate_outline(
                session, material_id=material_id, content_json=content_json
            )

            await job_repo.update_status(job_id, "complete")

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

            from course_supporter.storage.material_entry_repository import (
                MaterialEntryRepository,
            )

            entry_repo = MaterialEntryRepository(session)
            await entry_repo.fail_processing(material_id, error_message=error_message)

            # Extension point: update blocking_factors on mappings
            await self._revalidate_blocked_mappings(session, material_id=material_id)

            await session.commit()

        log.info("ingestion_callback_failure", error=error_message)

    # ------------------------------------------------------------------
    # Outline generation (best-effort)
    # ------------------------------------------------------------------

    async def _generate_outline(
        self,
        session: AsyncSession,
        *,
        material_id: uuid.UUID,
        content_json: str,
    ) -> None:
        """Generate MaterialOutline from processed content (best-effort).

        Failures are logged but do not propagate — ArchitectAgent falls
        back to raw ``processed_content`` when ``outline_content`` is None.
        """
        if self._router is None:
            return

        from course_supporter.agents.outline import OutlineAgent
        from course_supporter.models.source import SourceDocument
        from course_supporter.storage.material_entry_repository import (
            MaterialEntryRepository,
        )
        from course_supporter.storage.orm import ExternalServiceCall

        log = logger.bind(material_id=str(material_id))

        try:
            source_doc = SourceDocument.model_validate_json(content_json)
            agent = OutlineAgent(self._router)
            result = await agent.run_with_metadata(source_doc)

            entry_repo = MaterialEntryRepository(session)
            await entry_repo.save_outline(
                material_id,
                outline_json=result.outline.model_dump_json(),
            )

            session.add(
                ExternalServiceCall(
                    action="material_outline",
                    provider=result.response.provider,
                    model_id=result.response.model_id,
                    prompt_ref=result.prompt_version,
                    unit_type="tokens",
                    unit_in=result.response.tokens_in,
                    unit_out=result.response.tokens_out,
                    latency_ms=result.response.latency_ms,
                    cost_usd=result.response.cost_usd,
                    success=True,
                )
            )

            log.info(
                "outline_generated",
                model=result.response.model_id,
                sections=len(result.outline.sections),
                tokens_in=result.response.tokens_in,
                tokens_out=result.response.tokens_out,
            )
        except Exception as exc:
            log.warning("outline_generation_failed", error=str(exc))

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
