"""Background tasks for async processing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.architect import ArchitectAgent, NodePosition
from course_supporter.agents.reconciler import ReconcileAgent
from course_supporter.agents.refine import RefineAgent
from course_supporter.ingestion.factory import (
    create_heavy_steps,
    create_processors,
    create_vd_pipeline,
)
from course_supporter.models.source import SourceType
from course_supporter.models.step import ChildSnapshotContext, NodeSummary
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.snapshot_repository import SnapshotRepository

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.models.course import MaterialNodeSummary, SlideTimecodeRef
    from course_supporter.models.source import SourceDocument
    from course_supporter.models.step import (
        Correction,
        StepInput,
        StepOutput,
        StepType,
    )
    from course_supporter.storage.orm import (
        MaterialNode,
        StructureNodeEditable,
        StructureSnapshot,
    )
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
    callback = IngestionCallback(session_factory, router=router)

    log = structlog.get_logger().bind(
        job_id=job_id, material_id=material_id, source_type=source_type
    )
    log.info("ingestion_started")

    heavy = create_heavy_steps(router=router)
    vd = create_vd_pipeline()

    from course_supporter.config import get_settings
    from course_supporter.stt.setup import create_stt_router

    stt_router = create_stt_router(get_settings(), session_factory)
    processors = create_processors(heavy, vd_pipeline=vd, stt_router=stt_router)
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
    *,
    allow_empty: bool = False,
) -> list[SourceDocument]:
    """Extract SourceDocuments from READY MaterialEntries.

    Args:
        flat_nodes: Flat list of nodes with materials loaded.
        allow_empty: If True, return empty list instead of raising
            when no READY entries found. Used for parent nodes that
            rely on children snapshots for context.

    Returns:
        Deserialized SourceDocument list.

    Raises:
        NoReadyMaterialsError: If no READY entries found and allow_empty is False.
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

    if not documents and not allow_empty:
        msg = "No READY materials found for generation"
        raise NoReadyMaterialsError(msg)
    return documents


def _collect_outline_context(
    flat_nodes: list[MaterialNode],
) -> str | None:
    """Collect outline_content from READY entries, if any exist.

    Parses each outline JSON to ensure validity, then serializes
    as a single object (one outline) or JSON array (multiple).
    Returns None if no outlines are available.
    """
    import json

    import structlog

    from course_supporter.storage.orm import MaterialState

    log = structlog.get_logger()
    parsed: list[dict[str, object]] = []
    for node in flat_nodes:
        for entry in node.materials:
            if entry.state == MaterialState.READY and entry.outline_content:
                try:
                    parsed.append(json.loads(entry.outline_content))
                except json.JSONDecodeError:
                    log.warning(
                        "invalid_outline_content",
                        entry_id=str(entry.id),
                    )

    if not parsed:
        return None
    if len(parsed) == 1:
        return json.dumps(parsed[0], ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False)


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
    from course_supporter.fingerprint import FingerprintService
    from course_supporter.ingestion.merge import MergeStep
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )

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
            outline_context = _collect_outline_context(flat_nodes)
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
                context,
                existing_structure=existing_structure,
                outline_context=outline_context,
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

            # Auto-init editable tree from new snapshot
            editable_repo = EditableRepository(session)
            await editable_repo.init_from_snapshot(
                snapshot_id=snapshot.id,
                materialnode_id=effective_node_id,
                preserve_edited=True,
            )

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


def _snapshot_to_summary(
    node: MaterialNode,
    snap: StructureSnapshot,
) -> NodeSummary:
    """Convert a MaterialNode + its latest snapshot into NodeSummary."""
    return NodeSummary(
        node_id=node.id,
        title=node.title,
        summary=snap.summary or "",
        core_concepts=snap.core_concepts or [],
        mentioned_concepts=snap.mentioned_concepts or [],
        structure_snapshot_id=snap.id,
    )


def _determine_node_position(node: MaterialNode) -> NodePosition:
    """Determine node position in the material tree hierarchy.

    Args:
        node: MaterialNode with children relationship loaded.

    Returns:
        NodePosition.ROOT if no parent, LEAF if no children,
        INTERMEDIATE otherwise.
    """
    if node.parent_materialnode_id is None:
        return NodePosition.ROOT
    if not node.children:
        return NodePosition.LEAF
    return NodePosition.INTERMEDIATE


async def _load_children_summaries(
    session: AsyncSession,
    node: MaterialNode,
) -> list[NodeSummary]:
    """Load NodeSummary list from latest snapshots of child nodes.

    Args:
        session: Active DB session.
        node: Parent MaterialNode (children relationship loaded).

    Returns:
        List of NodeSummary for children that have snapshots.
    """
    if not node.children:
        return []

    child_ids = [c.id for c in node.children]
    snap_repo = SnapshotRepository(session)
    latest = await snap_repo.get_latest_for_nodes(child_ids)

    summaries: list[NodeSummary] = []
    for child in node.children:
        snap = latest.get(child.id)
        if snap is None or snap.summary is None:
            continue
        summaries.append(_snapshot_to_summary(child, snap))
    return summaries


async def _load_children_snapshots(
    session: AsyncSession,
    node: MaterialNode,
) -> list[ChildSnapshotContext]:
    """Load full snapshot data from child nodes for parent context.

    Used for context compression: parent nodes receive child snapshots
    (structure + summary + concepts + summary_nested_nodes) instead of
    raw descendant materials.

    Args:
        session: Active DB session.
        node: Parent MaterialNode (children relationship loaded).

    Returns:
        List of ChildSnapshotContext for children that have snapshots.
    """
    if not node.children:
        return []

    child_ids = [c.id for c in node.children]
    snap_repo = SnapshotRepository(session)
    latest = await snap_repo.get_latest_for_nodes(child_ids)

    snapshots: list[ChildSnapshotContext] = []
    for child in node.children:
        snap = latest.get(child.id)
        if snap is None or snap.structure is None:
            continue
        snapshots.append(
            ChildSnapshotContext(
                node_id=child.id,
                title=child.title,
                structure=snap.structure,
                summary=snap.summary or "",
                core_concepts=snap.core_concepts or [],
                mentioned_concepts=snap.mentioned_concepts or [],
                summary_nested_nodes=snap.summary_nested_nodes or "",
            )
        )
    return snapshots


async def _load_parent_context(
    session: AsyncSession,
    node: MaterialNode,
) -> NodeSummary | None:
    """Load NodeSummary for the parent node from its latest snapshot.

    Args:
        session: Active DB session.
        node: Current MaterialNode (parent relationship loaded).

    Returns:
        NodeSummary of the parent, or None if root or no snapshot.
    """
    if node.parent_materialnode_id is None or node.parent is None:
        return None

    snap_repo = SnapshotRepository(session)
    snap = await snap_repo.get_latest_for_node(node.parent_materialnode_id)
    if snap is None or snap.summary is None:
        return None

    return _snapshot_to_summary(node.parent, snap)


async def _load_sibling_summaries(
    session: AsyncSession,
    node: MaterialNode,
) -> list[NodeSummary]:
    """Load NodeSummary list for sibling nodes (same parent, excluding self).

    Args:
        session: Active DB session.
        node: Current MaterialNode (parent relationship loaded).

    Returns:
        List of NodeSummary for siblings that have snapshots.
    """
    if node.parent is None:
        return []

    siblings = [c for c in node.parent.children if c.id != node.id]
    if not siblings:
        return []

    sibling_ids = [s.id for s in siblings]
    snap_repo = SnapshotRepository(session)
    latest = await snap_repo.get_latest_for_nodes(sibling_ids)

    summaries: list[NodeSummary] = []
    for sib in siblings:
        snap = latest.get(sib.id)
        if snap is None or snap.summary is None:
            continue
        summaries.append(_snapshot_to_summary(sib, snap))
    return summaries


def _build_step_input(
    *,
    effective_node_id: uuid.UUID,
    step_type: StepType,
    documents: list[SourceDocument],
    mappings: list[SlideTimecodeRef],
    tree_summary: list[MaterialNodeSummary],
    flat_nodes: list[MaterialNode],
    mode: Literal["free", "guided"],
    children_summaries: list[NodeSummary] | None = None,
    parent_context: NodeSummary | None = None,
    sibling_summaries: list[NodeSummary] | None = None,
    children_snapshots: list[ChildSnapshotContext] | None = None,
    outline_context: str | None = None,
) -> StepInput:
    """Assemble StepInput from collected tree data.

    Builds existing_structure for guided mode via tree serialization.
    Sliding window context: children summaries (bottom-up), parent
    context and sibling summaries (top-down reconciliation).
    Children snapshots provide full snapshot data for context compression.
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
        children_summaries=children_summaries or [],
        parent_context=parent_context,
        sibling_summaries=sibling_summaries or [],
        existing_structure=existing_structure,
        mode=mode,
        material_tree=tree_summary,
        slide_timecode_refs=mappings,
        children_snapshots=children_snapshots or [],
        outline_context=outline_context,
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
        summary_nested_nodes=step_output.summary_nested_nodes or None,
    )

    sn_nodes = convert_to_structure_nodes(step_output.structure, snapshot.id)
    sn_repo = StructureNodeRepository(session)
    await sn_repo.create_tree(sn_nodes)

    # Auto-init editable tree from new snapshot
    editable_repo = EditableRepository(session)
    await editable_repo.init_from_snapshot(
        snapshot_id=snapshot.id,
        materialnode_id=effective_node_id,
        preserve_edited=True,
    )

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
    from course_supporter.fingerprint import FingerprintService
    from course_supporter.models.step import StepType as _StepType
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )

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

            # Resolve target node for context loading.
            # target is None when nid is None (whole-tree mode); root is the target.
            target_node = target if target is not None else root_nodes[0]

            # Context compression: load child snapshots for parent nodes.
            # If children have snapshots, parent uses own materials only.
            children_snap = await _load_children_snapshots(session, target_node)

            if children_snap:
                # Parent node: only its own materials (not subtree).
                # Parent may have no own materials — that's OK when
                # children snapshots provide the context.
                documents = _collect_ready_documents([target_node], allow_empty=True)
                outline_ctx = _collect_outline_context([target_node])
            else:
                # Leaf node (or children without snapshots): all subtree materials
                documents = _collect_ready_documents(flat_nodes)
                outline_ctx = _collect_outline_context(flat_nodes)

            mappings = _collect_validated_mappings(flat_nodes)

            from course_supporter.tree_utils import build_material_tree_summary

            tree_summary = build_material_tree_summary(flat_nodes)

            # Load sliding window context from previous steps.
            # Skip children_summaries when full snapshots are available —
            # they would be redundant and inflate the prompt.
            children_summaries: list[NodeSummary] = []
            if not children_snap:
                children_summaries = await _load_children_summaries(
                    session, target_node
                )

            # Parent + sibling context for reconcile/refine steps
            parent_context: NodeSummary | None = None
            sibling_sums: list[NodeSummary] = []
            if st in (_StepType.RECONCILE, _StepType.REFINE):
                parent_context = await _load_parent_context(session, target_node)
                sibling_sums = await _load_sibling_summaries(session, target_node)

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
                children_summaries=children_summaries,
                parent_context=parent_context,
                sibling_summaries=sibling_sums,
                children_snapshots=children_snap,
                outline_context=outline_ctx,
            )

            agent: ArchitectAgent | ReconcileAgent | RefineAgent
            if st == _StepType.RECONCILE:
                agent = ReconcileAgent(router, mode=mode)
            elif st == _StepType.REFINE:
                agent = RefineAgent(router, mode=mode)
            else:
                node_position = _determine_node_position(target_node)
                agent = ArchitectAgent(router, mode=mode, node_position=node_position)
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


async def arq_reconcile_preview(
    ctx: dict[str, Any],
    job_id: str,
    node_id: str,
) -> None:
    """ARQ task: analyze editable tree for cross-node consistency issues.

    Loads editable tree, serializes to JSON, calls LLM via ReconcileAgent,
    and stores the preview result on the Job record.

    Args:
        ctx: ARQ worker context (session_factory, model_router).
        job_id: Job UUID as string.
        node_id: MaterialNode UUID whose editable tree to analyze.
    """
    from course_supporter.api.routes.reconciliation import _editable_tree_to_dicts
    from course_supporter.storage.job_repository import JobRepository

    jid = uuid.UUID(job_id)
    nid = uuid.UUID(node_id)

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(job_id=job_id, node_id=node_id)
    log.info("reconcile_preview_started")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await session.commit()

            # Load editable tree
            editable_repo = EditableRepository(session)
            flat = await editable_repo.get_tree(nid)

            if not flat:
                msg = "No editable tree found. Generate a structure first."
                raise ValueError(msg)

            tree_dicts = _editable_tree_to_dicts(flat)

            # Call LLM via ReconcileAgent
            agent = ReconcileAgent(router, strategy="default")
            preview = await agent.preview(tree_dicts)

            # Store result on Job (backward compat)
            result_data = {
                "issues": [issue.model_dump(mode="json") for issue in preview.issues],
                "context_summary": preview.context_summary,
            }
            await job_repo.store_result(jid, result_data)

            # Persist in ReconciliationPreview cache if fingerprint available
            job = await job_repo.get_by_id(jid)
            params = job.input_params if job and job.input_params else {}
            combined_fp = params.get("combined_fingerprint")
            node_fp = params.get("node_fingerprint")
            editable_hash = params.get("editable_tree_hash")

            if combined_fp and node_fp and editable_hash:
                from course_supporter.storage.reconciliation_preview_repository import (
                    ReconciliationPreviewRepository,
                )

                rp_repo = ReconciliationPreviewRepository(session)
                issues_list = cast(list[dict[str, Any]], result_data["issues"])
                await rp_repo.upsert(
                    materialnode_id=nid,
                    combined_fingerprint=str(combined_fp),
                    node_fingerprint=str(node_fp),
                    editable_tree_hash=str(editable_hash),
                    issues=issues_list,
                    context_summary=preview.context_summary,
                    job_id=jid,
                )

            await job_repo.update_status(jid, "complete")
            await session.commit()
            log.info(
                "reconcile_preview_done",
                issues_count=len(preview.issues),
            )

        except Exception as exc:
            await session.rollback()
            async with session_factory() as err_session:
                err_repo = JobRepository(err_session)
                await err_repo.update_status(
                    jid,
                    "failed",
                    error_message=str(exc),
                )
                await err_session.commit()
            log.error("reconcile_preview_failed", error=str(exc))


async def arq_execute_methodist_step(
    ctx: dict[str, Any],
    job_id: str,
    materialnode_id: str,
    editable_id: str,
    phase: Literal["bottom_up", "top_down"] = "bottom_up",
) -> None:
    """ARQ task: execute a Methodist step for a single editable node.

    Loads the editable node's context (outline, structure metadata,
    sliding window), runs MethodistAgent, and persists output
    (JSONB + Markdown) on the StructureNodeEditable.

    Args:
        ctx: ARQ worker context (session_factory, model_router).
        job_id: Job UUID as string.
        materialnode_id: Root MaterialNode UUID as string.
        editable_id: StructureNodeEditable UUID to process.
        phase: 'bottom_up' or 'top_down'.
    """
    import json

    from course_supporter.agents.methodist import (
        MethodistAgent,
        format_material_roles,
        format_methodist_children,
        format_methodist_parent,
        format_methodist_siblings,
    )
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.material_entry_repository import (
        MaterialEntryRepository,
    )
    from course_supporter.storage.material_node_repository import (
        MaterialNodeRepository,
    )

    jid = uuid.UUID(job_id)
    mn_id = uuid.UUID(materialnode_id)
    ed_id = uuid.UUID(editable_id)

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]

    log = structlog.get_logger().bind(
        job_id=job_id,
        editable_id=editable_id,
        phase=phase,
    )
    log.info("methodist_step_started")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        try:
            await job_repo.update_status(jid, "active")
            await session.commit()

            # 1. Load editable node and its tree
            editable_repo = EditableRepository(session)
            target = await editable_repo.get_by_id(ed_id)
            if target is None:
                msg = f"Editable node {ed_id} not found"
                raise ValueError(msg)

            all_editables = await editable_repo.get_tree(mn_id)

            # Build editable lookup and relationships
            ed_map = {ed.id: ed for ed in all_editables}
            children = [ed for ed in all_editables if ed.parent_editable_id == ed_id]
            siblings = [
                ed
                for ed in all_editables
                if ed.parent_editable_id == target.parent_editable_id and ed.id != ed_id
            ]
            parent = (
                ed_map.get(target.parent_editable_id)
                if target.parent_editable_id
                else None
            )

            # 2. Determine node position
            is_root = target.parent_editable_id is None
            has_children = bool(children)
            node_position: Literal["leaf", "intermediate", "root"]
            if is_root:
                node_position = "root"
            elif has_children:
                node_position = "intermediate"
            else:
                node_position = "leaf"

            # 3. Collect outline context from MaterialEntries
            node_repo = MaterialNodeRepository(session)
            mat_node = await node_repo.get_by_id(mn_id)
            outline_ctx = ""
            material_roles_info: list[tuple[str, str, str]] = []
            if mat_node is not None:
                entry_repo = MaterialEntryRepository(session)
                entries = await entry_repo.get_for_node(mn_id)
                outlines = []
                for entry in entries:
                    material_roles_info.append(
                        (
                            entry.filename or entry.source_url,
                            entry.source_type,
                            entry.material_role,
                        )
                    )
                    if entry.outline_content:
                        try:
                            outlines.append(
                                json.loads(entry.outline_content),
                            )
                        except json.JSONDecodeError:
                            log.warning(
                                "malformed_outline_content",
                                entry_id=str(entry.id),
                            )
                if outlines:
                    outline_ctx = json.dumps(
                        outlines,
                        ensure_ascii=False,
                    )

            # 4. Build structure context from target editable
            structure_ctx = json.dumps(
                {
                    "title": target.title,
                    "description": target.description,
                    "node_type": target.node_type,
                    "learning_goal": target.learning_goal,
                    "key_concepts": target.key_concepts,
                    "common_mistakes": target.common_mistakes,
                    "success_criteria": target.success_criteria,
                },
                ensure_ascii=False,
            )

            # 5. Build sliding window context
            def _node_summary(
                ed: StructureNodeEditable,
            ) -> NodeSummary:
                return NodeSummary(
                    node_id=ed.id,
                    title=ed.title,
                    summary=ed.description or "",
                    # key_concepts is JSONB list[dict[str, str]] per ORM schema
                    core_concepts=[c.get("name", "") for c in (ed.key_concepts or [])],
                    mentioned_concepts=[],
                    structure_snapshot_id=ed.source_snapshot_id,
                )

            parent_ctx = format_methodist_parent(
                _node_summary(parent) if parent else None,
            )
            sibling_ctx = format_methodist_siblings(
                [_node_summary(s) for s in siblings],
            )
            children_ctx = format_methodist_children(
                [_node_summary(c) for c in children],
            )
            roles_ctx = format_material_roles(material_roles_info)

            # 6. Run Methodist agent
            agent = MethodistAgent(router)
            result = await agent.run_with_metadata(
                node_title=target.title,
                node_description=target.description or "",
                node_type=target.node_type,
                node_position=node_position,
                outline_context=outline_ctx or "{}",
                structure_context=structure_ctx,
                parent_context=parent_ctx,
                sibling_context=sibling_ctx,
                children_context=children_ctx,
                material_roles=roles_ctx,
            )

            # 7. Persist output on editable node
            from course_supporter.storage.orm import ExternalServiceCall

            esc = ExternalServiceCall(
                tenant_id=mat_node.tenant_id if mat_node else None,
                job_id=jid,
                action="methodist",
                strategy="default",
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
            session.add(esc)
            await session.flush()

            target.methodological_content = result.output.model_dump(mode="json")
            target.methodological_markdown = result.output.rendered_markdown
            target.methodist_call_id = esc.id

            await job_repo.update_status(jid, "complete")
            await session.commit()
            log.info(
                "methodist_step_done",
                model=result.response.model_id,
                objectives=len(result.output.learning_objectives),
                gaps=len(result.output.gaps),
            )

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
                log.info(
                    "cascading_failure_propagated",
                    failed_count=len(cascaded),
                )
            log.error("methodist_step_failed", error=str(exc))
