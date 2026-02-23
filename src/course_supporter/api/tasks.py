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
    from course_supporter.models.course import SlideTimecodeRef
    from course_supporter.models.source import SourceDocument
    from course_supporter.storage.orm import MaterialNode, SourceMaterial
    from course_supporter.storage.s3 import S3Client


class _HasSourceUrl(Protocol):
    source_url: str


class _MaterialProxy:
    """Lightweight proxy that overrides source_url without touching the ORM."""

    __slots__ = ("_source_url", "_wrapped")

    def __init__(self, wrapped: _HasSourceUrl, source_url: str) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_source_url", source_url)

    @property
    def source_url(self) -> str:
        url: str = object.__getattribute__(self, "_source_url")
        return url

    def __getattr__(self, name: str) -> object:
        result: object = getattr(object.__getattribute__(self, "_wrapped"), name)
        return result


@asynccontextmanager
async def _resolve_s3_url(
    material: SourceMaterial,
    s3: S3Client | None,
) -> AsyncIterator[SourceMaterial]:
    """Download S3 object to temp file, yield a proxy with local path.

    The original ORM object is **never mutated**, preventing accidental
    auto-flush of a temp path to the database.

    Yields the original *material* unchanged when the URL is not an S3
    URL, or a lightweight proxy with ``source_url`` pointing to the
    downloaded temp file otherwise.
    """
    s3_key = s3.extract_key(material.source_url) if s3 else None
    temp_path: Path | None = None

    try:
        if s3 and s3_key:
            temp_path = await s3.download_file(s3_key)
            proxy = _MaterialProxy(material, str(temp_path))
            yield proxy  # type: ignore[misc]
        else:
            yield material
    finally:
        if temp_path is not None:
            try:
                ap = anyio.Path(temp_path)
                if await ap.exists():
                    await ap.unlink(missing_ok=True)
            except Exception:
                log = structlog.get_logger()
                log.warning("s3_temp_cleanup_failed", path=str(temp_path))


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

            async with _resolve_s3_url(material, s3) as resolved:
                doc = await processor.process(resolved, router=router)

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


def _resolve_target_nodes(
    root_nodes: list[MaterialNode],
    course_id: uuid.UUID,
    node_id: uuid.UUID | None,
) -> tuple[MaterialNode | None, list[MaterialNode]]:
    """Resolve target node and flatten its subtree.

    Thin wrapper around :func:`tree_utils.resolve_target_nodes`.
    """
    from course_supporter.tree_utils import resolve_target_nodes

    return resolve_target_nodes(root_nodes, course_id, node_id)


def _collect_ready_documents(
    flat_nodes: list[MaterialNode],
) -> list[SourceDocument]:
    """Extract SourceDocuments from READY MaterialEntries.

    Args:
        flat_nodes: Flat list of nodes with materials loaded.

    Returns:
        Deserialized SourceDocument list.

    Raises:
        NoReadyMaterialsError: If no READY entries found.
    """
    from course_supporter.errors import NoReadyMaterialsError
    from course_supporter.models.source import SourceDocument
    from course_supporter.storage.orm import MaterialState

    documents: list[SourceDocument] = []
    for node in flat_nodes:
        for entry in node.materials:
            if entry.state == MaterialState.READY:
                documents.append(
                    SourceDocument.model_validate_json(
                        entry.processed_content,  # type: ignore[arg-type]
                    )
                )

    if not documents:
        msg = "No READY materials found for generation"
        raise NoReadyMaterialsError(msg)
    return documents


def _collect_validated_mappings(
    flat_nodes: list[MaterialNode],
) -> list[SlideTimecodeRef]:
    """Extract SlideTimecodeRef from validated SlideVideoMappings.

    Args:
        flat_nodes: Flat list of nodes with slide_video_mappings loaded.

    Returns:
        List of SlideTimecodeRef (may be empty).
    """
    from course_supporter.models.course import SlideTimecodeRef
    from course_supporter.storage.orm import MappingValidationState

    mappings: list[SlideTimecodeRef] = []
    for node in flat_nodes:
        for svm in node.slide_video_mappings:
            if svm.validation_state == MappingValidationState.VALIDATED:
                mappings.append(
                    SlideTimecodeRef(
                        slide_number=svm.slide_number,
                        video_timecode_start=svm.video_timecode_start,
                    )
                )
    return mappings


async def arq_generate_structure(
    ctx: dict[str, Any],
    job_id: str,
    course_id: str,
    node_id: str | None = None,
    mode: str = "free",
) -> None:
    """ARQ task: generate course structure via ArchitectAgent.

    Loads READY materials from subtree (or full tree), merges into
    CourseContext, calls LLM, and saves snapshot. Idempotent —
    skips LLM call if a snapshot with the same fingerprint exists.

    Args:
        ctx: ARQ worker context (session_factory, model_router).
        job_id: Job UUID as string (ARQ JSON serialization).
        course_id: Course UUID as string.
        node_id: Optional target node UUID. None = whole course.
        mode: Generation mode ('free' or 'guided').
    """
    from course_supporter.agents.architect import ArchitectAgent
    from course_supporter.fingerprint import FingerprintService
    from course_supporter.ingestion.merge import MergeStep
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )
    from course_supporter.storage.snapshot_repository import SnapshotRepository

    jid = uuid.UUID(job_id)
    cid = uuid.UUID(course_id)
    nid = uuid.UUID(node_id) if node_id else None

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(
        job_id=job_id,
        course_id=course_id,
        node_id=node_id,
        mode=mode,
    )
    log.info("generate_structure_started")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await session.commit()

            # Load tree → resolve target → flatten
            node_repo = MaterialNodeRepository(session)
            root_nodes: list[MaterialNode] = await node_repo.get_tree(
                cid,
                include_materials=True,
            )
            target, flat_nodes = _resolve_target_nodes(root_nodes, cid, nid)

            # Collect data for generation
            documents = _collect_ready_documents(flat_nodes)
            mappings = _collect_validated_mappings(flat_nodes)

            # Merge
            context = MergeStep().merge(
                documents,
                mappings if mappings else None,
            )

            # Compute fingerprint
            fp_service = FingerprintService(session)
            if target is not None:
                fingerprint = await fp_service.ensure_node_fp(target)
            else:
                fingerprint = await fp_service.ensure_course_fp(root_nodes)
            await session.commit()

            # Idempotency check
            snap_repo = SnapshotRepository(session)
            existing = await snap_repo.find_by_identity(
                course_id=cid,
                node_id=nid,
                node_fingerprint=fingerprint,
                mode=mode,
            )
            if existing is not None:
                log.info("generate_structure_idempotent", snapshot_id=str(existing.id))
                await job_repo.update_status(
                    jid,
                    "complete",
                    result_snapshot_id=existing.id,
                )
                await session.commit()
                return

            # Generate via ArchitectAgent
            agent = ArchitectAgent(router)
            gen_result = await agent.run_with_metadata(context)

            # Save snapshot
            snapshot = await snap_repo.create(
                course_id=cid,
                node_id=nid,
                node_fingerprint=fingerprint,
                mode=mode,
                structure=gen_result.structure.model_dump(),
                prompt_version=gen_result.prompt_version,
                model_id=gen_result.response.model_id,
                tokens_in=gen_result.response.tokens_in,
                tokens_out=gen_result.response.tokens_out,
                cost_usd=gen_result.response.cost_usd,
            )

            # Job → complete
            await job_repo.update_status(
                jid,
                "complete",
                result_snapshot_id=snapshot.id,
            )
            await session.commit()
            log.info("generate_structure_done", snapshot_id=str(snapshot.id))

        except Exception as exc:
            await session.rollback()
            async with session_factory() as err_session:
                await JobRepository(err_session).update_status(
                    jid,
                    "failed",
                    error_message=str(exc),
                )
                await err_session.commit()
            log.error("generate_structure_failed", error=str(exc))


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

            async with _resolve_s3_url(material, s3) as resolved:
                doc = await processor.process(resolved, router=router)

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
