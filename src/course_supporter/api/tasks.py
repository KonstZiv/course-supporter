"""Background tasks for async processing."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video import VideoProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.models.source import SourceType
from course_supporter.storage.repositories import SourceMaterialRepository

logger = structlog.get_logger()

PROCESSOR_MAP = {
    SourceType.VIDEO: VideoProcessor,
    SourceType.PRESENTATION: PresentationProcessor,
    SourceType.TEXT: TextProcessor,
    SourceType.WEB: WebProcessor,
}


async def ingest_material(
    material_id: uuid.UUID,
    source_type: str,
    source_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Process a source material in the background.

    1. Transitions status to 'processing'.
    2. Selects processor by source_type.
    3. Processes the source URL.
    4. Saves content snapshot and transitions to 'done'.
    5. On error, transitions to 'error' with message.

    Args:
        material_id: UUID of the SourceMaterial record.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or path to the source file.
        session_factory: Async session factory for DB access.
    """
    log = logger.bind(material_id=str(material_id), source_type=source_type)
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

            processor = processor_cls()
            doc = await processor.process(source_url)

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
