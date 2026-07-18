"""Post-ingestion DOMAIN reaction on the AuthoredDocument (L2).

The ``Job.status`` lifecycle is owned by the execution seam
(:func:`~course_supporter.jobs.execution_seam.through_seam`); this service no
longer writes it. It carries only the domain half around an ingestion outcome:

1. AuthoredDocument processing state (``complete_processing`` / ``fail_processing``).
2. Best-effort failure cleanup — the orphan-Summary observer and the Stage 2
   reject soft-delete.
3. Merkle-fingerprint invalidation (inside the soft-delete cascade).

``on_success`` runs in the task body before the seam's ``complete`` write;
``on_failure`` runs from the seam's opaque post-terminal callback AFTER the
durable ``failed`` write (terminal-first). Each provides its own session via the
caller's session_factory.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

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
    ) -> None:
        """Handle successful ingestion — the DOMAIN half only (L2).

        Marks the AuthoredDocument ``complete_processing``. The ``Job.status`` →
        ``complete`` transition is owned by the execution seam
        (:func:`~course_supporter.jobs.execution_seam.through_seam`). This runs in
        the task body BEFORE the seam writes ``complete``, so the material is
        durably completed before the Job is — no stuck-``pending`` window on the
        success path.

        Args:
            job_id: The Job tracking this ingestion (log context only).
            material_id: The material that was processed.
        """
        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            from course_supporter.storage.authored_document_repository import (
                AuthoredDocumentRepository,
            )

            entry_repo = AuthoredDocumentRepository(session)
            await entry_repo.complete_processing(material_id)
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
        """Handle failed ingestion — the DOMAIN half only (L2).

        The ``Job.status`` → ``failed`` transition (+ error_message/category) is
        owned by the execution seam, which writes it DURABLY before invoking this
        reaction (terminal-first — a stranded ``active`` would block the single
        serial worker's queue). This method carries only the domain half:
        material visibility (``fail_processing``, its own guarded commit) plus the
        best-effort orphan observer / Stage 2 reject soft-delete. When
        ``error_category == ErrorCategory.STAGE2_REJECTED``
        (Phase 2.1 C6 per KD-2.1-P) the failed AuthoredDocument is additionally
        soft-deleted so the operator-facing tree never displays a rejected
        document; infrastructure failures and Stage 1 rejections fall through the
        default branch (``fail_processing`` only, row intact).

        Args:
            job_id: The Job tracking this ingestion (log context only).
            material_id: The material that failed.
            error_message: Human-readable error description.
            error_category: Optional :class:`ErrorCategory`. Only
                ``STAGE2_REJECTED`` triggers the additional cascade
                soft-delete; all other values (and ``None``) preserve the
                ``fail_processing``-only flow.
        """
        log = structlog.get_logger().bind(
            job_id=str(job_id), material_id=str(material_id)
        )

        async with self._session_factory() as session:
            # Material visibility (task 3.3c-B, Vector 2): mark the
            # AuthoredDocument ERROR + persist its error_message in its OWN
            # guarded sub-step with its OWN commit, BEFORE the best-effort
            # cleanup. The Job terminal is the seam's (written durably before
            # this reaction — terminal-first); this is the material-visibility
            # half of "do not lose it" — a failure in the best-effort cleanup
            # below must not leave the material in an invisible `pending` state
            # (the UI would still show it processing).
            from course_supporter.storage.authored_document_repository import (
                AuthoredDocumentRepository,
            )

            try:
                entry_repo = AuthoredDocumentRepository(session)
                await entry_repo.fail_processing(
                    material_id,
                    error_message=error_message,
                    error_category=error_category,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                # log.exception preserves the traceback: this catch-all guards
                # an *unexpected* secondary failure (a bug we want to debug, not
                # swallow blind) — task 3.3c-B reviewer 💡-A.
                log.exception(
                    "ingestion_failure_material_visibility_skipped", error=str(exc)
                )

            # Best-effort cleanup (orphan observer, Stage 2 reject
            # soft-delete). Guarded so a failure here can neither re-raise into
            # the already-returning ARQ task nor undo the durable terminal +
            # visibility writes above (task 3.3c-B, Vector 2). Only
            # genuinely-secondary bookkeeping lives here.
            try:
                # Orphan-Summary observer (task 2.4.6, Q4 / DD-2.4-G). Pass 2a
                # commits the DocumentSummary before process_detail runs
                # (api/tasks.py), so a later Pass 2b/2c failure leaves that
                # summary committed under a now-ERROR document. This is a
                # pre-existing cross-source_type window (Phase 2.1+); here we
                # only OBSERVE it (read-only warning in this already-open
                # failure session) so the condition is visible before task
                # 2.4.7's Pass 2c introduces a real process_detail failure
                # surface. Resolution (single-commit / rollback / accept) is
                # deferred to DD-2.4-G; the orchestrator commit sequence is
                # NOT touched.
                from course_supporter.storage.document_summary_repository import (
                    DocumentSummaryRepository,
                )

                orphan = await DocumentSummaryRepository(
                    session
                ).get_by_authored_document_id(material_id)
                if orphan is not None:
                    log.warning(
                        "ingestion_orphan_summary_detected",
                        summary_id=str(orphan.id),
                        error=error_message,
                    )

                from course_supporter.security.exceptions import ErrorCategory

                if error_category == ErrorCategory.STAGE2_REJECTED:
                    await self._soft_delete_rejected_document(
                        session, material_id=material_id, log=log
                    )

                await session.commit()
            except Exception as exc:
                # The terminal + visibility writes are already durable; secondary
                # bookkeeping is best-effort and must not propagate. log.exception
                # keeps the traceback for debugging (task 3.3c-B reviewer 💡-A).
                await session.rollback()
                log.exception("ingestion_failure_secondary_skipped", error=str(exc))

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

        cascade_service = CascadeDeleteService(session)
        cascade_map = build_cascade_map(AuthoredDocument)
        content_hash_service = ContentHashService(session)
        job_cancellation_service = JobCancellationService(session)

        async def invalidate_hook(
            ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
        ) -> None:
            await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

        # L1b: direct bind — the rejected document's own id is in the victim
        # set and IS the job subject (JCS keys on subject_id); no
        # course_node_id augmentation needed.
        await cascade_service.soft_delete_with_cascade(
            document,
            cascade_map,
            on_cancel_jobs=job_cancellation_service.cancel_jobs_for_entities,
            on_invalidate_hashes=invalidate_hook,
        )
        log.info("stage2_reject_cascade_complete", material_id=str(material_id))
