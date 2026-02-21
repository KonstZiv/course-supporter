"""Background tasks for async processing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion.factory import create_heavy_steps, create_processors
from course_supporter.models.source import SourceType
from course_supporter.storage.repositories import SourceMaterialRepository

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.s3 import S3Client


class _HasSourceUrl(Protocol):
    source_url: str


@asynccontextmanager
async def _resolve_s3_url(
    material: _HasSourceUrl,
    s3: S3Client | None,
) -> AsyncIterator[None]:
    """Download S3 object to temp file, patch material URL, restore on exit.

    If ``material.source_url`` is not an S3 URL (or *s3* is ``None``),
    the context manager is a no-op.
    """
    original_url: str = material.source_url
    s3_key = s3.extract_key(original_url) if s3 else None
    temp_path: Path | None = None

    try:
        if s3 and s3_key:
            temp_path = await s3.download_file(s3_key)
            material.source_url = str(temp_path)
        yield
    finally:
        material.source_url = original_url
        if temp_path is not None:
            ap = anyio.Path(temp_path)
            if await ap.exists():
                await ap.unlink(missing_ok=True)


async def arq_ingest_material(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    material_id: str,  # UUID as string (ARQ JSON serialization)
    source_type: str,
    source_url: str,
    priority: str = "normal",
) -> None:
    """ARQ task: process a source material with job tracking.

    Thin orchestrator: validates priority, transitions to active,
    runs the processor, then delegates completion handling to
    :class:`~course_supporter.ingestion_callback.IngestionCallback`.

    Args:
        ctx: ARQ worker context (session_factory, model_router, engine).
        job_id: Job UUID as string (ARQ serializes via JSON).
        material_id: SourceMaterial UUID as string.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority ('normal' or 'immediate').
    """
    from course_supporter.ingestion_callback import IngestionCallback
    from course_supporter.job_priority import JobPriority, check_work_window
    from course_supporter.storage.job_repository import JobRepository

    check_work_window(JobPriority(priority))

    jid = uuid.UUID(job_id)
    mid = uuid.UUID(material_id)
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]
    callback = IngestionCallback(session_factory)

    log = structlog.get_logger().bind(
        job_id=job_id, material_id=material_id, source_type=source_type
    )
    log.info("ingestion_started")

    heavy = create_heavy_steps(router=router)
    processors = create_processors(heavy)
    s3: S3Client | None = ctx.get("s3_client")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        mat_repo = SourceMaterialRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await mat_repo.update_status(mid, "processing")
            await session.commit()

            try:
                st = SourceType(source_type)
                processor = processors[st]
            except (ValueError, KeyError):
                msg = f"Unsupported source_type: {source_type}"
                raise ValueError(msg) from None

            material = await mat_repo.get_by_id(mid)
            if material is None:
                msg = f"SourceMaterial not found: {mid}"
                raise ValueError(msg)

            async with _resolve_s3_url(material, s3):
                doc = await processor.process(material, router=router)

            content = doc.model_dump_json()

        except Exception as exc:
            await session.rollback()
            await callback.on_failure(
                job_id=jid, material_id=mid, error_message=str(exc)
            )
            log.error("ingestion_failed", error=str(exc))
            return

    await callback.on_success(job_id=jid, material_id=mid, content_json=content)
    log.info("ingestion_done")


async def ingest_material(
    material_id: uuid.UUID,
    source_type: str,
    source_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    router: ModelRouter | None = None,
    s3: S3Client | None = None,
) -> None:
    """Process a source material in the background (legacy).

    Kept for backward compatibility. New code should use
    :func:`arq_ingest_material` via the ARQ worker.
    """
    log = structlog.get_logger().bind(
        material_id=str(material_id), source_type=source_type
    )
    log.info("ingestion_started")

    heavy = create_heavy_steps(router=router)
    processors = create_processors(heavy)

    async with session_factory() as session:
        repo = SourceMaterialRepository(session)
        try:
            await repo.update_status(material_id, "processing")
            await session.commit()

            try:
                st = SourceType(source_type)
                processor = processors[st]
            except (ValueError, KeyError):
                msg = f"Unsupported source_type: {source_type}"
                raise ValueError(msg) from None

            material = await repo.get_by_id(material_id)
            if material is None:
                msg = f"SourceMaterial not found: {material_id}"
                raise ValueError(msg)

            async with _resolve_s3_url(material, s3):
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
