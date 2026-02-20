"""Background tasks for async processing."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType
from course_supporter.storage.repositories import SourceMaterialRepository

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter

PROCESSOR_MAP = {
    SourceType.VIDEO: VideoProcessor,
    SourceType.PRESENTATION: PresentationProcessor,
    SourceType.TEXT: TextProcessor,
    SourceType.WEB: WebProcessor,
}


async def arq_ingest_material(
    ctx: dict[str, Any],
    job_id: str,
    material_id: str,
    source_type: str,
    source_url: str,
    priority: str = "normal",
) -> None:
    """ARQ task: process a source material with job tracking.

    Called by the ARQ worker. Manages Job status transitions
    alongside SourceMaterial processing.

    Args:
        ctx: ARQ worker context (session_factory, model_router, engine).
        job_id: Job UUID as string (ARQ serializes via JSON).
        material_id: SourceMaterial UUID as string.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority ('normal' or 'immediate').
    """
    from course_supporter.job_priority import JobPriority, check_work_window
    from course_supporter.storage.job_repository import JobRepository

    check_work_window(JobPriority(priority))

    jid = uuid.UUID(job_id)
    mid = uuid.UUID(material_id)
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(
        job_id=job_id, material_id=material_id, source_type=source_type
    )
    log.info("ingestion_started")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        mat_repo = SourceMaterialRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await mat_repo.update_status(mid, "processing")
            await session.commit()

            processor_cls = PROCESSOR_MAP.get(SourceType(source_type))
            if processor_cls is None:
                msg = f"No processor for source_type: {source_type}"
                raise ValueError(msg)

            material = await mat_repo.get_by_id(mid)
            if material is None:
                msg = f"SourceMaterial not found: {mid}"
                raise ValueError(msg)

            processor = processor_cls()
            doc = await processor.process(material, router=router)

            content = doc.model_dump_json()
            await mat_repo.update_status(mid, "done", content_snapshot=content)
            await job_repo.update_status(jid, "complete", result_material_id=mid)
            await session.commit()
            log.info("ingestion_done")

        except Exception as exc:
            await session.rollback()

            async with session_factory() as error_session:
                err_job_repo = JobRepository(error_session)
                err_mat_repo = SourceMaterialRepository(error_session)
                await err_job_repo.update_status(jid, "failed", error_message=str(exc))
                await err_mat_repo.update_status(mid, "error", error_message=str(exc))
                await error_session.commit()

            log.error("ingestion_failed", error=str(exc))


async def ingest_material(
    material_id: uuid.UUID,
    source_type: str,
    source_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    router: ModelRouter | None = None,
) -> None:
    """Process a source material in the background (legacy).

    Kept for backward compatibility. New code should use
    :func:`arq_ingest_material` via the ARQ worker.
    """
    log = structlog.get_logger().bind(
        material_id=str(material_id), source_type=source_type
    )
    log.info("ingestion_started")

    async with session_factory() as session:
        repo = SourceMaterialRepository(session)
        try:
            await repo.update_status(material_id, "processing")
            await session.commit()

            processor_cls = PROCESSOR_MAP.get(SourceType(source_type))
            if processor_cls is None:
                msg = f"No processor for source_type: {source_type}"
                raise ValueError(msg)

            material = await repo.get_by_id(material_id)
            if material is None:
                msg = f"SourceMaterial not found: {material_id}"
                raise ValueError(msg)

            processor = processor_cls()
            doc = await processor.process(material, router=router)

            content = doc.model_dump_json()
            await repo.update_status(material_id, "done", content_snapshot=content)
            await session.commit()
            log.info("ingestion_done")

        except Exception as exc:
            await session.rollback()

            async with session_factory() as error_session:
                error_repo = SourceMaterialRepository(error_session)
                await error_repo.update_status(
                    material_id,
                    "error",
                    error_message=str(exc),
                )
                await error_session.commit()

            log.error("ingestion_failed", error=str(exc))
