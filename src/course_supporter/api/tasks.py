"""Background tasks for async processing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion.factory import create_heavy_steps, create_processors
from course_supporter.models.source import SourceType

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.models.course import SlideTimecodeRef
    from course_supporter.models.source import SourceDocument
    from course_supporter.models.step import Correction, StepInput, StepOutput, StepType
    from course_supporter.storage.orm import MaterialNode
    from course_supporter.storage.s3 import S3Client
    from course_supporter.storage.snapshot_repository import SnapshotRepository


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
    material: _HasSourceUrl,
    s3: S3Client | None,
) -> AsyncIterator[Any]:  # Any: processor.process() expects MaterialEntry
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
            yield proxy
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
    """ARQ task: process a MaterialEntry with job tracking.

    Thin orchestrator: validates priority, transitions to active,
    runs the processor, then delegates completion handling to
    :class:`~course_supporter.ingestion_callback.IngestionCallback`.

    Args:
        ctx: ARQ worker context (session_factory, model_router, engine).
        job_id: Job UUID as string (ARQ serializes via JSON).
        material_id: MaterialEntry UUID as string.
        source_type: One of 'video', 'presentation', 'text', 'web'.
        source_url: URL or S3 path to the source file.
        priority: Job priority ('normal' or 'immediate').
    """
    from course_supporter.ingestion_callback import IngestionCallback
    from course_supporter.job_priority import JobPriority, check_work_window
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_entry_repository import (
        MaterialEntryRepository,
    )

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
        entry_repo = MaterialEntryRepository(session)

        entry = await entry_repo.get_by_id(mid)
        if entry is None:
            log.error("material_entry_not_found", material_id=material_id)
            return

        try:
            await job_repo.update_status(jid, "active")
            await entry_repo.set_pending(mid, jid)
            await session.commit()

            try:
                st = SourceType(source_type)
                processor = processors[st]
            except (ValueError, KeyError):
                msg = f"Unsupported source_type: {source_type}"
                raise ValueError(msg) from None

            async with _resolve_s3_url(entry, s3) as resolved:
                doc = await processor.process(resolved, router=router)

            content = doc.model_dump_json()

        except Exception as exc:
            await session.rollback()
            await callback.on_failure(
                job_id=jid,
                material_id=mid,
                error_message=str(exc),
            )
            log.error("ingestion_failed", error=str(exc))
            return

    await callback.on_success(
        job_id=jid,
        material_id=mid,
        content_json=content,
    )
    log.info("ingestion_done")


def _resolve_target_nodes(
    root_nodes: list[MaterialNode],
    node_id: uuid.UUID | None,
) -> tuple[MaterialNode | None, list[MaterialNode]]:
    """Resolve target node and flatten its subtree.

    Thin wrapper around :func:`tree_utils.resolve_target_nodes`.
    """
    from course_supporter.tree_utils import resolve_target_nodes

    return resolve_target_nodes(root_nodes, node_id)


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
    root_node_id: str,
    target_node_id: str | None = None,
    mode: Literal["free", "guided"] = "free",
) -> None:
    """ARQ task: generate course structure via ArchitectAgent.

    Loads READY materials from subtree (or full tree), merges into
    CourseContext, calls LLM, and saves snapshot. Idempotent —
    skips LLM call if a snapshot with the same fingerprint exists.

    Args:
        ctx: ARQ worker context (session_factory, model_router).
        job_id: Job UUID as string (ARQ JSON serialization).
        root_node_id: Root MaterialNode UUID as string.
        target_node_id: Optional target node UUID. None = whole tree.
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
    rid = uuid.UUID(root_node_id)
    nid = uuid.UUID(target_node_id) if target_node_id else None

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(
        job_id=job_id,
        root_node_id=root_node_id,
        target_node_id=target_node_id,
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
            root_nodes: list[MaterialNode] = await node_repo.get_subtree(
                rid,
                include_materials=True,
            )
            target, flat_nodes = _resolve_target_nodes(root_nodes, nid)

            # Collect data for generation
            documents = _collect_ready_documents(flat_nodes)
            mappings = _collect_validated_mappings(flat_nodes)

            # Build tree summary for LLM context
            from course_supporter.tree_utils import build_material_tree_summary

            tree_summary = build_material_tree_summary(flat_nodes)

            # Merge
            context = MergeStep().merge(
                documents,
                mappings if mappings else None,
                material_tree=tree_summary,
            )

            # Compute fingerprint
            fp_service = FingerprintService(session)
            if target is not None:
                fingerprint = await fp_service.ensure_node_fp(target)
            else:
                fingerprint = await fp_service.ensure_course_fp(root_nodes)
            await session.commit()

            # Effective node_id for snapshot identity
            effective_node_id = nid or rid

            # Idempotency check
            snap_repo = SnapshotRepository(session)
            existing = await snap_repo.find_by_identity(
                node_id=effective_node_id,
                node_fingerprint=fingerprint,
                mode=mode,
            )
            if existing is not None:
                log.info("generate_structure_idempotent", snapshot_id=str(existing.id))
                await job_repo.update_status(jid, "complete")
                await session.commit()
                return

            # Generate via ArchitectAgent
            from course_supporter.storage.orm import ExternalServiceCall
            from course_supporter.tree_utils import serialize_tree_for_guided

            existing_structure = (
                serialize_tree_for_guided(flat_nodes) if mode == "guided" else None
            )
            agent = ArchitectAgent(router, mode=mode)
            gen_result = await agent.run_with_metadata(
                context, existing_structure=existing_structure
            )

            # Persist LLM metadata as ExternalServiceCall
            esc = ExternalServiceCall(
                action="course_structuring",
                strategy=mode,
                provider=gen_result.response.provider,
                model_id=gen_result.response.model_id,
                prompt_ref=gen_result.prompt_version,
                unit_type="tokens",
                unit_in=gen_result.response.tokens_in,
                unit_out=gen_result.response.tokens_out,
                latency_ms=gen_result.response.latency_ms,
                cost_usd=gen_result.response.cost_usd,
                success=True,
            )
            session.add(esc)
            await session.flush()

            # Save snapshot with ESC FK
            snapshot = await snap_repo.create(
                node_id=effective_node_id,
                node_fingerprint=fingerprint,
                mode=mode,
                structure=gen_result.structure.model_dump(),
                externalservicecall_id=esc.id,
            )

            # Convert LLM output → StructureNode tree and persist
            from course_supporter.storage.structure_node_repository import (
                StructureNodeRepository,
            )
            from course_supporter.structure_conversion import (
                convert_to_structure_nodes,
            )

            sn_nodes = convert_to_structure_nodes(gen_result.structure, snapshot.id)
            sn_repo = StructureNodeRepository(session)
            await sn_repo.create_tree(sn_nodes)

            # Job → complete
            await job_repo.update_status(jid, "complete")
            await session.commit()
            log.info("generate_structure_done", snapshot_id=str(snapshot.id))

        except Exception as exc:
            await session.rollback()
            async with session_factory() as err_session:
                err_repo = JobRepository(err_session)
                await err_repo.update_status(
                    jid,
                    "failed",
                    error_message=str(exc),
                )
                cascaded = await err_repo.propagate_failure(jid)
                await err_session.commit()
            if cascaded:
                log.info("cascading_failure_propagated", failed_count=len(cascaded))
            log.error("generate_structure_failed", error=str(exc))


def _build_step_input(
    *,
    effective_node_id: uuid.UUID,
    step_type: StepType,
    documents: list[SourceDocument],
    mappings: list[SlideTimecodeRef],
    tree_summary: list[Any],
    flat_nodes: list[MaterialNode],
    mode: Literal["free", "guided"],
) -> StepInput:
    """Assemble StepInput from collected tree data.

    Builds existing_structure for guided mode via tree serialization.
    Children/parent/sibling summaries are empty until S3-020b adds
    the sliding window context.
    """
    from course_supporter.models.step import StepInput as _StepInput
    from course_supporter.tree_utils import serialize_tree_for_guided

    existing_structure = (
        serialize_tree_for_guided(flat_nodes) if mode == "guided" else None
    )
    return _StepInput(
        node_id=effective_node_id,
        step_type=step_type,
        materials=documents,
        children_summaries=[],
        parent_context=None,
        sibling_summaries=[],
        existing_structure=existing_structure,
        mode=mode,
        material_tree=tree_summary,
        slide_timecode_refs=mappings,
    )


def _serialize_corrections(
    corrections: list[Correction] | None,
) -> list[dict[str, Any]] | None:
    """Convert Correction dataclasses to JSON-serializable dicts."""
    if not corrections:
        return None
    return [
        {
            "target_node_id": str(c.target_node_id),
            "field": c.field,
            "action": c.action,
            "old_value": c.old_value,
            "new_value": c.new_value,
            "reason": c.reason,
        }
        for c in corrections
    ]


async def _persist_step_result(
    session: AsyncSession,
    step_output: StepOutput,
    *,
    effective_node_id: uuid.UUID,
    fingerprint: str,
    mode: str,
    step_type: str,
    snap_repo: SnapshotRepository,
) -> uuid.UUID:
    """Persist ExternalServiceCall, snapshot, and StructureNodes.

    Returns:
        Created snapshot ID.
    """
    from course_supporter.storage.orm import ExternalServiceCall
    from course_supporter.storage.structure_node_repository import (
        StructureNodeRepository,
    )
    from course_supporter.structure_conversion import convert_to_structure_nodes

    esc = ExternalServiceCall(
        action="course_structuring",
        strategy=mode,
        provider=step_output.response.provider,
        model_id=step_output.response.model_id,
        prompt_ref=step_output.prompt_version,
        unit_type="tokens",
        unit_in=step_output.response.tokens_in,
        unit_out=step_output.response.tokens_out,
        latency_ms=step_output.response.latency_ms,
        cost_usd=step_output.response.cost_usd,
        success=True,
    )
    session.add(esc)
    await session.flush()

    snapshot = await snap_repo.create(
        node_id=effective_node_id,
        node_fingerprint=fingerprint,
        mode=mode,
        structure=step_output.structure.model_dump(),
        externalservicecall_id=esc.id,
        step_type=step_type,
        summary=step_output.summary,
        core_concepts=step_output.core_concepts,
        mentioned_concepts=step_output.mentioned_concepts,
        corrections=_serialize_corrections(step_output.corrections),
    )

    sn_nodes = convert_to_structure_nodes(step_output.structure, snapshot.id)
    sn_repo = StructureNodeRepository(session)
    await sn_repo.create_tree(sn_nodes)

    return snapshot.id


async def arq_execute_step(
    ctx: dict[str, Any],
    job_id: str,
    root_node_id: str,
    target_node_id: str | None = None,
    mode: Literal["free", "guided"] = "free",
    step_type: str = "generate",
) -> None:
    """ARQ task: execute a generation step using StepInput/StepOutput contracts.

    Generic Step Executor that builds StepInput, delegates to the
    appropriate Agent, and persists StepOutput fields in the snapshot.

    Currently supports step_type="generate" only; reconcile/refine
    will be added in S3-020c/d.

    Args:
        ctx: ARQ worker context (session_factory, model_router).
        job_id: Job UUID as string (ARQ JSON serialization).
        root_node_id: Root MaterialNode UUID as string.
        target_node_id: Optional target node UUID. None = whole tree.
        mode: Generation mode ('free' or 'guided').
        step_type: Step type ('generate', 'reconcile', 'refine').
    """
    from course_supporter.agents.architect import ArchitectAgent
    from course_supporter.fingerprint import FingerprintService
    from course_supporter.models.step import StepType as _StepType
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )
    from course_supporter.storage.snapshot_repository import SnapshotRepository

    jid = uuid.UUID(job_id)
    rid = uuid.UUID(root_node_id)
    nid = uuid.UUID(target_node_id) if target_node_id else None
    st = _StepType(step_type)

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(
        job_id=job_id,
        root_node_id=root_node_id,
        target_node_id=target_node_id,
        mode=mode,
        step_type=step_type,
    )
    log.info("execute_step_started")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await session.commit()

            # Load tree → resolve target → flatten
            node_repo = MaterialNodeRepository(session)
            root_nodes: list[MaterialNode] = await node_repo.get_subtree(
                rid,
                include_materials=True,
            )
            target, flat_nodes = _resolve_target_nodes(root_nodes, nid)

            # Collect data
            documents = _collect_ready_documents(flat_nodes)
            mappings = _collect_validated_mappings(flat_nodes)

            from course_supporter.tree_utils import build_material_tree_summary

            tree_summary = build_material_tree_summary(flat_nodes)

            # Compute fingerprint
            fp_service = FingerprintService(session)
            if target is not None:
                fingerprint = await fp_service.ensure_node_fp(target)
            else:
                fingerprint = await fp_service.ensure_course_fp(root_nodes)
            await session.commit()

            effective_node_id = nid or rid

            # Idempotency check
            snap_repo = SnapshotRepository(session)
            existing = await snap_repo.find_by_identity(
                node_id=effective_node_id,
                node_fingerprint=fingerprint,
                mode=mode,
            )
            if existing is not None:
                log.info("execute_step_idempotent", snapshot_id=str(existing.id))
                await job_repo.update_status(jid, "complete")
                await session.commit()
                return

            # Build StepInput → execute Agent → persist results
            step_input = _build_step_input(
                effective_node_id=effective_node_id,
                step_type=st,
                documents=documents,
                mappings=mappings,
                tree_summary=tree_summary,
                flat_nodes=flat_nodes,
                mode=mode,
            )

            agent = ArchitectAgent(router, mode=mode)
            step_output = await agent.execute(step_input)

            snapshot_id = await _persist_step_result(
                session,
                step_output,
                effective_node_id=effective_node_id,
                fingerprint=fingerprint,
                mode=mode,
                step_type=step_type,
                snap_repo=snap_repo,
            )

            await job_repo.update_status(jid, "complete")
            await session.commit()
            log.info("execute_step_done", snapshot_id=str(snapshot_id))

        except Exception as exc:
            await session.rollback()
            async with session_factory() as err_session:
                err_repo = JobRepository(err_session)
                await err_repo.update_status(
                    jid,
                    "failed",
                    error_message=str(exc),
                )
                cascaded = await err_repo.propagate_failure(jid)
                await err_session.commit()
            if cascaded:
                log.info("cascading_failure_propagated", failed_count=len(cascaded))
            log.error("execute_step_failed", error=str(exc))
