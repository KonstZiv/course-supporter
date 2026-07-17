"""Background tasks for async processing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.ingestion.factory import (
    create_heavy_steps,
    create_processors,
)
from course_supporter.jobs.execution_seam import SeamTerminal, through_seam
from course_supporter.models.source import SourceType
from course_supporter.service_logging import (
    reset_progress_writer,
    set_job_from_arq,
    set_progress_writer,
    set_tenant_from_job,
)

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from course_supporter.llm.router import ModelRouter
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
) -> AsyncIterator[Any]:  # Any: processor.process_raw() expects AuthoredDocument
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


# F6 backstop byte-cap on the Stage 2 safety surface (task-code-materials,
# operator-ratified 2026-07-12): 1.5 MiB keeps ~11% headroom under the
# 0.5-ratio 1M-rung estimator ceiling (0.5 * 1,048,576 tokens ~= 1.67 MiB
# ASCII at chars/3.5), absorbing the estimator's token-count variance.
# Normal oversize handling is the ladder's input_budget_ratio (skip to the
# 1M rung); this cap is last resort only and warns loudly with
# covered/total so the truncation is never silent.
_SAFETY_TEXT_MAX_BYTES: Final[int] = int(1.5 * 1024 * 1024)


def _bound_safety_text(text: str, *, log: Any) -> str:
    """Apply the F6 last-resort byte cap to the Stage 2 safety surface.

    Under the cap the text passes through byte-identical. Over the cap,
    Stage 2 sees the first 1.5 MiB (cut at a char boundary) and a loud
    warning records covered vs total bytes — the vetted-prefix /
    unvetted-tail divergence is a conscious, visible trade-off.
    """
    raw = text.encode("utf-8")
    if len(raw) <= _SAFETY_TEXT_MAX_BYTES:
        return text
    bounded = raw[:_SAFETY_TEXT_MAX_BYTES].decode("utf-8", errors="ignore")
    log.warning(
        "stage2_safety_text_truncated",
        covered_bytes=len(bounded.encode("utf-8")),
        total_bytes=len(raw),
        cap_bytes=_SAFETY_TEXT_MAX_BYTES,
    )
    return bounded


async def _ingest_on_terminal(
    ctx: dict[str, Any],
    job_id: str,
    material_id: str,
    *_rest: Any,
    outcome: SeamTerminal,
) -> None:
    """Ingest's opaque post-terminal domain reaction (L2, GO condition 3).

    The seam has already written the terminal Job status (durably, terminal-
    first) before this runs. Only the FAILURE path needs a reaction here:
    material visibility + the dependent-job cascade + the Stage 2 reject
    soft-delete, which MUST run after the failed-Job commit so a stranded
    ``active`` never blocks the serial worker. On success ``complete_processing``
    already ran in the body (before the seam's ``complete``); on ``obsolete`` the
    subject is already gone — neither needs a reaction here.
    """
    if outcome.status != "failed":
        return
    from course_supporter.ingestion_callback import IngestionCallback

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    await IngestionCallback(session_factory).on_failure(
        job_id=uuid.UUID(job_id),
        material_id=uuid.UUID(material_id),
        error_message=outcome.error_message or "",
        error_category=outcome.error_category,
    )


@through_seam(on_terminal=_ingest_on_terminal)
async def arq_ingest_material(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    material_id: str,  # UUID as string (ARQ JSON serialization)
    source_type: str,
    source_url: str,
    priority: str = "normal",
) -> None:
    """ARQ task body: process an AuthoredDocument through the two-pass pipeline.

    Wrapped by the L2 execution seam (:func:`through_seam`), which owns the
    ``Job.status`` lifecycle (active → complete on return / failed on raise) +
    the AuthoredDocument liveness check (soft-deleted material → ``obsolete``,
    body skipped). The body runs the pipeline and, on success, the domain
    ``complete_processing`` (before the seam's ``complete``); on failure it raises
    and the seam's post-terminal callback (:func:`_ingest_on_terminal`) runs the
    failure-domain cascade AFTER the durable ``failed`` write (terminal-first).

    Args:
        ctx: ARQ worker context (session_factory, model_router,
            stage_router, s3_client, engine, plus framework-injected
            ``redis`` ArqRedis pool used by AudioProcessor word-cache).
        job_id: Job UUID as string (ARQ serializes via JSON).
        material_id: AuthoredDocument UUID as string.
        source_type: One of 'video', 'presentation', 'text', 'web', 'audio'.
        source_url: URL or S3 path to the source file.
        priority: Job priority ('normal' or 'immediate').
    """
    # PresentationProcessor + persist_slide_webps drive the Phase 6 T3 (KD17)
    # slide-WebP persistence on the seam (presentation source_type only).
    from course_supporter.ingestion.presentation import PresentationProcessor
    from course_supporter.ingestion.slide_persist import persist_slide_webps
    from course_supporter.ingestion_callback import IngestionCallback
    from course_supporter.job_priority import JobPriority, check_work_window
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.security.exceptions import (
        ErrorCategory,
        SecurityRejectedError,
    )
    from course_supporter.security.schemas import CourseContext
    from course_supporter.security.stage2 import run_stage2_safety_check
    from course_supporter.storage.authored_document_repository import (
        AuthoredDocumentRepository,
    )
    from course_supporter.storage.course_node_repository import (
        CourseNodeRepository,
    )
    from course_supporter.storage.document_segment_repository import (
        DocumentSegmentRepository,
    )
    from course_supporter.storage.document_summary_repository import (
        DocumentSummaryRepository,
    )
    from course_supporter.storage.job_repository import JobRepository

    check_work_window(JobPriority(priority))

    jid = uuid.UUID(job_id)
    mid = uuid.UUID(material_id)
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    router: ModelRouter = ctx["model_router"]
    stage_router: StageRouter = ctx["stage_router"]
    redis: ArqRedis = ctx["redis"]
    callback = IngestionCallback(session_factory)

    log = structlog.get_logger().bind(
        job_id=job_id, material_id=material_id, source_type=source_type
    )

    await set_tenant_from_job(session_factory, jid)
    set_job_from_arq(jid)
    log.info("ingestion_started")

    heavy = create_heavy_steps()

    from course_supporter.config import get_settings
    from course_supporter.stt.setup import create_stt_router

    stt_router = create_stt_router(get_settings(), session_factory)
    processors = create_processors(
        heavy,
        stt_router=stt_router,
        redis=redis,
        stage_router=stage_router,
    )
    s3: S3Client | None = ctx.get("s3_client")

    async with session_factory() as session:
        job_repo = JobRepository(session)
        entry_repo = AuthoredDocumentRepository(session)
        node_repo = CourseNodeRepository(session)

        entry = await entry_repo.get_by_id(mid)
        if entry is None:
            # The seam's liveness check turns a soft-deleted subject into
            # ``obsolete`` before this body runs; a None here is the extreme race
            # of the row vanishing in between. Raise → the seam writes ``failed``
            # (a normal return would wrongly land on ``complete``).
            log.error("material_entry_not_found", material_id=material_id)
            msg = f"AuthoredDocument {material_id} vanished after liveness check"
            raise ValueError(msg)

        # Resolve effective language: entry override → course default → None.
        # Under the 2.4.13 invariant (CHECK ``course_nodes_root_language_required``)
        # the root always has ``default_language``, so inheritance fires before
        # STT runs and the entry is persisted with the root's language; STT
        # auto-detect is observational only (no cache-back since 2.4.21).
        if entry.language is None:
            root = await node_repo.get_root_for(entry.course_node_id)
            if root is not None and root.default_language:
                entry.language = root.default_language
                log.debug(
                    "language_inherited_from_course",
                    language=entry.language,
                    root_id=str(root.id),
                )

        try:
            # Job → active is owned by the seam (already written on entry). The
            # body only marks the material pending + links the job (domain).
            await entry_repo.set_pending(mid, jid)
            await session.commit()

            try:
                st = SourceType(source_type)
                processor = processors[st]
            except (ValueError, KeyError):
                msg = f"Unsupported source_type: {source_type}"
                raise ValueError(msg) from None

            # Stage-progress writer for the CPU-bound video detection stage
            # (Krok 3): its own short-lived session, scoped tightly around
            # ``process_raw`` (only ``detection.detect`` reads it, via the
            # progress ContextVar). Best-effort — a write failure is swallowed
            # so progress never fails the ingest.
            async def _detection_progress(current: int, total: int) -> None:
                try:
                    async with session_factory() as progress_session:
                        progress_repo = JobRepository(progress_session)
                        await progress_repo.update_stage(jid, "detecting")
                        await progress_repo.update_stage_progress(
                            jid,
                            {
                                "stage": "detecting",
                                "current": current,
                                "total": total,
                                "unit": "frames",
                            },
                        )
                        await progress_session.commit()
                except Exception:  # best-effort progress; never fail the ingest
                    log.warning(
                        "detection_progress_write_failed",
                        current=current,
                        total=total,
                        exc_info=True,
                    )

            progress_token = set_progress_writer(_detection_progress)
            try:
                async with _resolve_s3_url(entry, s3) as resolved:
                    doc = await processor.process_raw(resolved, router=router)
                    # Task 2.4.14 — propagate the resolved course language into
                    # the SourceDocument so Pass 2a can pin its output language
                    # to the course (NOT to the input). ``entry.language`` is
                    # the canonical 639-3 code already resolved above (entry
                    # override → root.default_language). Post task-2.4.13
                    # rooted courses always have it; ``None`` remains a
                    # defensive sentinel handled by the prompt fallback gate.
                    doc.language = entry.language
            finally:
                reset_progress_writer(progress_token)

            # ── Stage 2 — LLM safety check (Phase 2.1 C6, KD-2.1-P) ──
            # Defense-in-depth: authored raw text may carry prompt
            # injection, harmful content, or off-topic material. Stage 2
            # gates Pass 2a so the downstream mapping LLM never sees
            # un-vetted content. Both pass and reject outcomes persist
            # the verdict via ``store_safety_result`` (KD-2.1-P contract
            # — operators can audit even ratified rejects). Rejection
            # commits the row before raising so the row survives
            # rollback; ``IngestionCallback.on_failure`` then soft-
            # deletes the document in its fresh session.
            await job_repo.update_stage(jid, "checking_safety")

            root_node = await node_repo.get_root_for(entry.course_node_id)
            target_node = await node_repo.get_by_id(entry.course_node_id)
            course_context = CourseContext(
                course_title=(root_node.title if root_node else ""),
                course_description=(
                    root_node.description if root_node and root_node.description else ""
                ),
                node_title=(target_node.title if target_node else ""),
                node_description=(
                    target_node.description
                    if target_node and target_node.description
                    else ""
                ),
                outline_summary="",
            )

            # Stage 2 safety sees the full authored surface via safety_text().
            # For single-stream source types this equals assemble_text() (the
            # Pass 2a mapping reference that Pass 2b slices); for video it also
            # includes the visual-scene descriptions, which the narrowed
            # transcript-only assemble_text() reference excludes (task 2.4.5) —
            # so on-screen slide text/code is never un-vetted by Stage 2.
            #
            # F6 (task-code-materials, ratified): the normal oversize path is
            # ``input_budget_ratio: 0.5`` on the safety_check_authored ladder
            # — oversize input skips the 131k rungs and lands on the 1M rung.
            # The byte backstop below is LAST RESORT only: input beyond even
            # the 1M-rung budget is truncated to the first 1.5 MiB (~11%
            # headroom under the 0.5-ratio 1M-rung estimator ceiling) with a
            # loud covered/total warning — never a silent degradation.
            submission_text = _bound_safety_text(doc.safety_text(), log=log)
            safety_result = await run_stage2_safety_check(
                submission_text,
                router=stage_router,
                course_context=course_context,
                content_kind="authored",
            )
            await entry_repo.store_safety_result(
                mid, safety_result=safety_result.model_dump(mode="json")
            )
            await session.commit()

            if not safety_result.is_safe:
                log.warning(
                    "stage2_authored_rejected",
                    violations=[v.value for v in safety_result.violations],
                    confidence=safety_result.confidence,
                    reasoning=safety_result.reasoning,
                )
                raise SecurityRejectedError(
                    ErrorCategory.STAGE2_REJECTED,
                    safety_result.reasoning or "Stage 2 LLM safety rejection",
                )

            # ── Pass 2a — premium LLM mapping (Phase 2.1 C5, KD-2.1-A) ──
            # Writes Job.current_stage="extracting_structure"; routes the
            # parsed SourceDocument through pass_2a_mapping ladder; creates
            # the canonical DocumentSummary row with cascade content_hash
            # invalidation (KD-2.1-F closure inside repository.create()).
            # Segment drafts on summary_draft.segments are retained for
            # Phase 2.1 C7 (Pass 2b) consumption -- not materialised here.
            await job_repo.update_stage(jid, "extracting_structure")
            summary_draft = await processor.process_macro(doc, stage_router)

            # Fixup 2.1.7.2 — content_char_count derived server-side from
            # the canonical reference text (``doc.assemble_text()``). The
            # LLM no longer emits this value (anchor-bias mitigation per
            # Etap 0 forensic 2026-05-13). The full-cover invariant
            # (``segments[-1].end_pos == reference_text_length``) is now
            # enforced inside ``DocumentSummaryDraft`` via Pydantic
            # context, with the closure passed as StageRouter
            # ``response_validator`` translating ``ValidationError`` to
            # ``StructuralRetryError`` so the ladder retry mechanism
            # runs before terminal failure. By the time control reaches
            # this point, the draft is guaranteed coverage-correct.
            derived_char_count = len(doc.assemble_text())
            summary_repo = DocumentSummaryRepository(session)
            summary = await summary_repo.create(
                authored_document_id=entry.id,
                title=summary_draft.title,
                description=summary_draft.description,
                main_concepts=summary_draft.main_concepts,
                secondary_concepts=summary_draft.secondary_concepts,
                content_char_count=derived_char_count,
                # task-code-materials: value-generic — None for every
                # non-code processor; CodeProcessor sets the project tree.
                structure=summary_draft.structure,
            )
            log.info(
                "pass_2a_complete",
                summary_id=str(summary.id),
                segment_draft_count=len(summary_draft.segments),
            )
            # Persist Pass 2a outputs (DocumentSummary + cascade
            # content_hash). Project convention: each business unit
            # commits at end (see other arq_* tasks).
            # callback.on_success uses its own session for Job
            # lifecycle update.
            await session.commit()
            # ── /Pass 2a ──

            # ── Pass 2b — algorithmic slice (Phase 2.1 C7, KD-2.1-O) ──
            # Materialises DocumentSegment rows from the Pass 2a-emitted
            # drafts. For text/web the processor fills ``content`` via
            # slice over the canonical reference text (offsets line up
            # exactly with what the mapping LLM saw). Repository owns
            # cascade content_hash invalidation up through the parent
            # chain to the root CourseNode (KD-2.1-F symmetric with
            # Pass 2a). Commits inside the same business unit.
            await job_repo.update_stage(jid, "creating_segments")
            segment_drafts = await processor.process_detail(doc, summary_draft)
            segment_repo = DocumentSegmentRepository(session)
            segments = await segment_repo.create_batch(
                summary.id,
                segment_drafts,
                source_doc=doc,
            )
            log.info(
                "pass_2b_complete",
                summary_id=str(summary.id),
                segment_count=len(segments),
            )

            # ── Slide persistence (Phase 6 T3, KD17) — presentation only ──
            # Persist the transiently-rendered slides as WebP to S3 and record
            # the ordered keys on the AuthoredDocument, INSIDE this Pass 2b
            # business unit so the commit below makes segments + slide_keys
            # durable together (Q3 strict). A failure here propagates to the
            # outer ``except`` → rollback → ``on_failure`` soft-delete
            # (material-complete iff ingest-success). The processor is
            # untouched bar a read-only accessor (``rendered_slides``); the
            # LLM contour (KD-2.3-F) is byte-identical.
            if isinstance(processor, PresentationProcessor) and s3 is not None:
                if root_node is None:
                    msg = (
                        f"Presentation {material_id} has no root CourseNode; "
                        "cannot key slide objects"
                    )
                    raise ValueError(msg)
                slide_keys = await persist_slide_webps(
                    s3,
                    tenant_id=root_node.tenant_id,
                    node_id=entry.course_node_id,
                    document_id=entry.id,
                    slides=processor.rendered_slides,
                )
                await entry_repo.set_slide_keys(mid, slide_keys=slide_keys)
                log.info(
                    "slides_persisted",
                    material_id=material_id,
                    slide_count=len(slide_keys),
                )

            await session.commit()
            # ── /Pass 2b ──

            # ── /Pass 2b — the body's domain work is committed. ──

        except SecurityRejectedError as exc:
            # Stage 2 reject (KD-2.1-P): the ``safety_result`` row is already
            # committed (pre-raise), so the body-session rollback on exit is a
            # no-op for it. The seam writes Job ``failed`` with ``exc.category``
            # (STAGE2_REJECTED); its post-terminal callback runs the cascade
            # soft-delete AFTER that durable write (terminal-first).
            log.warning(
                "ingestion_security_rejected",
                category=exc.category.value,
                detail=exc.detail,
            )
            raise
        except Exception as exc:
            # The seam writes Job ``failed``; a CategorisedProcessingError carries
            # its structural code (``exc.category``), which the seam persists as
            # ``error_category`` (F4). The post-terminal callback marks the
            # material failed + runs the best-effort cascade.
            log.error("ingestion_failed", error=str(exc))
            raise

    # Domain success: mark the material complete BEFORE the seam writes Job
    # ``complete`` (material durably completed first — no stuck-``pending``
    # window). Job ``complete`` is the seam's, on this body's normal return.
    await callback.on_success(job_id=jid, material_id=mid)
    log.info("ingestion_done")


async def _notify_homework_failed(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    """Best-effort 'failed' webhook (sprint-mentor T7).

    Reloads the submission/student/tenant in the given (error-handling) session
    and delivers a failed-event webhook. The caller wraps this so a webhook
    error never masks the processing failure.
    """
    from course_supporter.homework.webhook import (
        build_failed_payload,
        deliver_webhook,
        resolve_webhook_url,
    )
    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.orm import Tenant
    from course_supporter.storage.student_repository import StudentRepository

    submission = await HomeworkRepository(session).get_by_id(submission_id)
    if submission is None:
        return
    student = await StudentRepository(session).get_by_id(submission.student_id)
    tenant = await session.get(Tenant, submission.tenant_id)
    webhook_url = resolve_webhook_url(submission, tenant)
    if not webhook_url or student is None:
        return
    payload = build_failed_payload(submission, student, reason=reason)
    await deliver_webhook(url=webhook_url, payload=payload, session=session)
    await session.commit()


@through_seam()
async def arq_process_homework(
    ctx: dict[str, Any],
    job_id: str,
    submission_id: str,
) -> None:
    """ARQ task body: process a homework submission through the pipeline.

    Wrapped by the L2 execution seam (:func:`through_seam`): the seam owns the
    ``Job.status`` lifecycle (active → complete on normal return / failed on
    raise) + the HomeworkSubmission liveness check (a soft-deleted submission →
    ``obsolete``, body skipped). The body owns ALL domain — the
    ``HomeworkSubmission`` status machine, webhooks, and the fresh error-session
    reaction (Рат.2 strict boundary). On a processing failure the body performs
    its domain reaction (submission → rejected/failed + failed webhook) and then
    RE-RAISES so the seam writes ``failed``. The webhook therefore fires just
    before the seam's terminal write (a conscious, accepted "webhook-before-
    terminal" inversion — see the module tests).

    Orchestrates the full homework pipeline (KD13 / KD15):
    safety → sanity → review → delivery. Safety and the sanity gate both
    short-circuit to a terminal (``rejected`` / ``mismatch``) + webhook; only
    submissions that clear both reach the Mentor review graph (T6).

    Args:
        ctx: ARQ worker context (session_factory, stage_router, s3_client).
        job_id: Job UUID as string.
        submission_id: HomeworkSubmission UUID as string.
    """
    from course_supporter.homework.project_submission import (
        process_project_submission,
    )
    from course_supporter.homework.review_graph import (
        build_mentor_review_service,
    )
    from course_supporter.homework.sanity_gate import build_sanity_gate_service
    from course_supporter.homework.webhook import (
        build_mismatch_payload,
        build_reviewed_payload,
        deliver_webhook,
        resolve_webhook_url,
    )
    from course_supporter.models.source import AssignmentType
    from course_supporter.normalizer.classify import denylist_prefix
    from course_supporter.security.archive import extract_submission_content
    from course_supporter.security.exceptions import SecurityRejectedError
    from course_supporter.security.schemas import (
        CourseContext,
        SecurityContext,
        Stage1RejectionResult,
    )
    from course_supporter.security.stage1 import run_stage1
    from course_supporter.security.stage2 import run_stage2_safety_check
    from course_supporter.storage.authored_document_repository import (
        AuthoredDocumentRepository,
    )
    from course_supporter.storage.course_node_repository import (
        CourseNodeRepository,
    )
    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.student_repository import StudentRepository

    jid = uuid.UUID(job_id)
    sid = uuid.UUID(submission_id)

    from course_supporter.llm.stage_router import StageRouter

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    stage_router: StageRouter = ctx["stage_router"]
    s3 = ctx["s3_client"]

    log = structlog.get_logger().bind(
        job_id=job_id,
        submission_id=submission_id,
    )
    await set_tenant_from_job(session_factory, jid)
    set_job_from_arq(jid)
    log.info("homework_processing_started")

    async with session_factory() as session:
        hw_repo = HomeworkRepository(session)
        node_repo = CourseNodeRepository(session)
        try:
            # Load submission. The seam already turned a soft-deleted submission
            # into `obsolete` (skip); a None here is the extreme race of the row
            # vanishing after the liveness check → raise → the seam writes failed.
            submission = await hw_repo.get_by_id(sid)
            if submission is None:
                msg = f"HomeworkSubmission {sid} not found"
                raise ValueError(msg)

            # Load related entities for webhook delivery
            from course_supporter.storage.orm import Tenant

            student_repo = StudentRepository(session)
            student = await student_repo.get_by_id(submission.student_id)
            tenant = await session.get(Tenant, submission.tenant_id)

            # Enrich logger with tenant/student context
            log = log.bind(
                tenant_id=str(submission.tenant_id),
                student_id=str(submission.student_id),
                file_url=submission.file_url,
            )
            sec_ctx = SecurityContext(
                tenant_id=submission.tenant_id,
                student_id=submission.student_id,
                submission_id=sid,
                file_url=submission.file_url,
            )

            # Job → active is the seam's (already written on entry).
            # --- HW-004: Safety check (runs while status is still 'received';
            # sets the 'safety_ok' milestone only on pass, KD15 §1298) ---
            s3_key = s3.extract_key(submission.file_url)
            if s3_key is None:
                msg = f"Cannot extract S3 key from {submission.file_url}"
                raise ValueError(msg)

            file_path = await s3.download_file(s3_key)
            log.info("homework_file_downloaded", path=str(file_path))
            try:
                # Build course context for the safety check
                course_node = await node_repo.get_by_id(submission.course_node_id)
                target_node = await node_repo.get_by_id(submission.node_id)

                if not course_node:
                    log.warning(
                        "course_node_not_found",
                        course_node_id=str(submission.course_node_id),
                    )
                if not target_node:
                    log.warning(
                        "target_node_not_found",
                        node_id=str(submission.node_id),
                    )

                course_ctx = CourseContext(
                    course_title=course_node.title if course_node else "",
                    course_description=(
                        course_node.description or "" if course_node else ""
                    ),
                    node_title=target_node.title if target_node else "",
                    node_description=(
                        target_node.description or "" if target_node else ""
                    ),
                )

                # --- KD18 P3: branch on task_type ---
                # A project submission bypasses the single-file path's
                # extract_submission_content + run_stage1 (fail-closed on a real
                # project's non-allowlisted files — the same wall P2's base hit,
                # 1A) and is normalized via the classify normalizer instead,
                # BEFORE safety. Non-project submissions are byte-unchanged.
                file_bytes = file_path.read_bytes()
                task_doc = await AuthoredDocumentRepository(session).get_by_id(
                    submission.authored_document_id
                )
                is_project = (
                    task_doc is not None
                    and task_doc.task_type == AssignmentType.PROJECT.value
                )

                if is_project:
                    project_text = await process_project_submission(
                        session=session,
                        s3=s3,
                        hw_repo=hw_repo,
                        submission=submission,
                        sid=sid,
                        jid=jid,
                        file_bytes=file_bytes,
                        raw_key=s3_key,
                    )
                    if project_text is None:
                        # Fail-closed rejection persisted inside; the finally
                        # cleans the temp file. P4 will assemble the real Mentor
                        # delta context; here the interim text feeds safety.
                        return
                    submission_text = project_text
                else:
                    # Extract content (handles archives)
                    content = await extract_submission_content(file_path)

                    # Log non-fatal security warnings at WARNING level
                    for sw in content.security_warnings:
                        log.warning(
                            "security_warning",
                            **sw.as_log_dict(),
                        )

                    log.info(
                        "homework_content_extracted",
                        files=len(content.files),
                        total_size=content.total_size,
                        security_warnings=len(content.security_warnings),
                    )

                    # --- KD14 Stage 1 — synchronous validation ---
                    # HOMEWORK_POLICY caps at 1 MB so the in-memory read above is
                    # safe (per Phase 1.2 §6.2 option a ratify).
                    try:
                        # №14: denylist junk inside a student zip
                        # (__MACOSX/, node_modules/ …) is skipped before
                        # accounting instead of fail-closing the whole
                        # submission on a mac-packed archive.
                        stage1_result = run_stage1(
                            filename=submission.original_filename or file_path.name,
                            content=file_bytes,
                            context="homework",
                            archive_skip_matcher=denylist_prefix,
                        )
                    except SecurityRejectedError as stage1_exc:
                        # Stage 1 rejection persists as Stage1RejectionResult
                        # (synthetic shape; ``source='stage1'`` discriminates from
                        # Stage 2 SafetyResult per KD-1.2-I).
                        rejection = Stage1RejectionResult(
                            category=stage1_exc.category,
                            detail=stage1_exc.detail,
                        )
                        await hw_repo.store_safety_result(
                            sid, rejection.model_dump(mode="json")
                        )
                        await hw_repo.update_status(
                            sid, "rejected", error_message=stage1_exc.detail
                        )
                        await session.commit()
                        log.warning(
                            "homework_rejected_stage1",
                            category=stage1_exc.category.value,
                            detail=stage1_exc.detail,
                        )
                        return

                    # --- KD14 Stage 2 — LLM safety classifier (canonical) ---
                    # Assemble submission_text per Stage 1 output shape:
                    # archive_entries → concatenate entries with separators
                    #   (legacy SubmissionContent.full_text parity);
                    # nfc_text → use directly (NFC-normalized text body);
                    # both None (binary like PDF) → best-effort UTF-8 decode
                    #   (legacy ``safety/archive._read_text_file`` parity).
                    if stage1_result.archive_entries is not None:
                        submission_text = "\n".join(
                            f"--- {entry.arcname} ---\n"
                            f"{entry.content.decode('utf-8', errors='replace')}"
                            for entry in stage1_result.archive_entries
                        )
                    elif stage1_result.nfc_text is not None:
                        submission_text = stage1_result.nfc_text
                    else:
                        submission_text = file_bytes.decode("utf-8", errors="replace")

                # Caller-side observability log (KD-1.2-H Variant A; pairs
                # with StageRouter's ``stage_router_executing`` line).
                log.info(
                    "homework_safety_check_executing",
                    policy_context="homework",
                )
                safety_result = await run_stage2_safety_check(
                    submission_text=submission_text,
                    router=stage_router,
                    course_context=course_ctx,
                )
                await hw_repo.store_safety_result(
                    sid, safety_result.model_dump(mode="json")
                )
                await session.commit()

                if not safety_result.is_safe:
                    await hw_repo.update_status(
                        sid,
                        "rejected",
                        error_message=safety_result.reasoning,
                    )
                    await session.commit()
                    log.warning(
                        "homework_rejected_safety",
                        reasoning=safety_result.reasoning,
                        violations=[v.value for v in safety_result.violations],
                    )
                    return

                # Safety gate passed.
                await hw_repo.update_status(sid, "safety_ok")
                await session.commit()

                # --- sanity stage — lightweight validity gate (T7, KD15
                # §1336-1339) ---
                # Cheap binary classifier: does the submission look like an
                # attempt at the declared task? An economy gate before the
                # expensive review graph — NOT task matching (the submission is
                # anchored to its task, KD15). A high-confidence mismatch is
                # terminal (ratified A3-conservative); match or a low-confidence
                # mismatch passes through to review unchanged (no signal injected
                # into the graph context).
                sanity_service = build_sanity_gate_service(session, stage_router)
                sanity_outcome = await sanity_service.evaluate(
                    submission=submission,
                    submission_text=submission_text,
                )
                await hw_repo.store_sanity_result(
                    sid, sanity_outcome.classification.model_dump(mode="json")
                )
                await session.commit()

                if sanity_outcome.gated:
                    reason = sanity_outcome.classification.reason
                    await hw_repo.update_status(sid, "mismatch", error_message=reason)
                    await session.commit()
                    log.info(
                        "homework_sanity_mismatch",
                        confidence=sanity_outcome.classification.confidence,
                        reason=reason,
                    )
                    webhook_url = resolve_webhook_url(submission, tenant)
                    if webhook_url and student:
                        mismatch_payload = build_mismatch_payload(
                            submission, student, reason=reason
                        )
                        await deliver_webhook(
                            url=webhook_url,
                            payload=mismatch_payload,
                            session=session,
                        )
                        await session.commit()
                    return

                # Sanity gate passed.
                await hw_repo.update_status(sid, "sanity_ok")
                await session.commit()

                # --- review stage — Mentor review graph (sprint-mentor T6) ---
                # The three-layer graph (vision §"Mentor review як граф",
                # D8-D12) judges the submission on the node/course/industry
                # layers, aggregates + denoises against the student's history,
                # and synthesises one human review. Only submissions that
                # cleared both safety and the sanity gate reach here.
                await hw_repo.update_status(sid, "reviewing")
                await session.commit()

                # Refresh so the graph reads fresh attributes after the safety
                # commits expired the in-memory submission.
                await session.refresh(submission)
                review_service = build_mentor_review_service(session, stage_router)
                review_output = await review_service.review(
                    submission=submission,
                    submission_text=submission_text,
                )
                await hw_repo.store_review_result(
                    sid,
                    result=review_output.review_result.model_dump(mode="json"),
                    review_markdown=review_output.review_markdown,
                    score=review_output.score,
                )
                await session.commit()

                log.info(
                    "homework_review_complete",
                    score=review_output.score,
                    passed=review_output.review_result.verdict.passed,
                    correctness=review_output.review_result.verdict.correctness,
                )

                # --- HW-007b: Webhook — reviewed notification ---
                await hw_repo.update_status(sid, "completed")
                await session.commit()

                webhook_url = resolve_webhook_url(submission, tenant)
                if webhook_url and student:
                    # Refresh submission to pick up stored review data
                    await session.refresh(submission)
                    reviewed_payload = build_reviewed_payload(
                        submission,
                        student,
                    )
                    delivered = await deliver_webhook(
                        url=webhook_url,
                        payload=reviewed_payload,
                        session=session,
                    )
                    if delivered:
                        await hw_repo.update_status(sid, "delivered")

                # Commit the 'delivered' status write (if any); Job → complete is
                # the seam's, on this body's normal return.
                await session.commit()
                log.info("homework_processing_done")
            finally:
                if file_path.exists():
                    file_path.unlink()
                    log.debug("homework_temp_file_cleaned", path=str(file_path))

        except SecurityRejectedError as exc:
            exc.enrich(sec_ctx)
            await session.rollback()
            # Domain reaction in a fresh session (the main one rolled back): mark
            # the submission rejected. Job → failed is the seam's, on the re-raise
            # below — for homework this inverts terminal-first (the submission
            # write lands just before the Job terminal), a conscious accepted
            # degradation (Рат.2).
            async with session_factory() as err_session:
                err_hw_repo = HomeworkRepository(err_session)
                try:
                    await err_hw_repo.update_status(
                        sid,
                        "rejected",
                        error_message=str(exc),
                    )
                except ValueError as status_exc:
                    log.warning(
                        "homework_status_update_skipped",
                        reason=str(status_exc),
                    )
                await err_session.commit()
            log.warning(
                "security_violation",
                **exc.as_log_dict(),
            )
            raise

        except Exception as exc:
            await session.rollback()
            # Domain reaction in a fresh session: mark the submission failed +
            # best-effort failed webhook. Job → failed is the seam's, on the
            # re-raise below (webhook-before-terminal inversion, Рат.2).
            async with session_factory() as err_session:
                err_hw_repo = HomeworkRepository(err_session)
                failed_set = False
                try:
                    await err_hw_repo.update_status(
                        sid, "failed", error_message=str(exc)
                    )
                    failed_set = True
                except ValueError as status_exc:
                    log.warning(
                        "homework_status_update_skipped",
                        reason=str(status_exc),
                    )
                await err_session.commit()
                # Best-effort 'failed' webhook (T7) — only when we actually moved
                # the submission to 'failed' (skip if it was already terminal,
                # which carries its own webhook). A webhook error must never mask
                # the processing failure.
                if failed_set:
                    try:
                        await _notify_homework_failed(err_session, sid, reason=str(exc))
                    except Exception:
                        log.warning("homework_failed_webhook_skipped", exc_info=True)
            log.error("homework_processing_failed", error=str(exc))
            raise


@through_seam()
async def arq_regenerate_node_summary(
    ctx: dict[str, Any],
    job_id: str,
    vertex_node_id: str,
    force: bool = False,
) -> None:
    """ARQ task body: drive the two-pass methodist generation orchestrator.

    Wrapped by the L2 execution seam (:func:`through_seam`), which owns the
    ``Job.status`` lifecycle (active → complete on return / failed on raise) and
    the vertex-node liveness check (a soft-deleted vertex → ``obsolete``, this
    body skipped). The body is therefore pure domain: ContextVar plumbing so
    every ESC row resolves the caller's tenant/job, then ``orch.run`` to
    completion. Per-node errors are recorded into ``Job.stage_progress.errors[]``
    by the orchestrator regardless; a raised exception becomes the seam's
    ``failed`` terminal (the body's session rolls back on exit, the seam writes
    the terminal in its own fresh session).

    The single production-shaped entry point for ``orch.run()`` (Phase 3.2.4
    invariant 4 — routes never invoke the orchestrator synchronously).

    Args:
        ctx: ARQ worker context (session_factory, stage_router).
        job_id: Job UUID as string (ARQ serialises via JSON).
        vertex_node_id: Vertex CourseNode UUID as string — the root of
            the subtree the run will visit.
        force: The K1-ratified meaning here is informational only —
            the 422 decision on ``uncovered_stale_node_ids`` lives
            in the calling route. By the time this task runs the
            decision has already been made (route either raised
            422 or accepted), so the task carries ``force`` through
            to ``orch.run()`` purely for diagnostic / resume-replay
            symmetry. Memo-skip on both axes is unconditional.
    """
    from course_supporter.agents.methodist_factory import (
        build_node_summary_orchestrator,
    )
    from course_supporter.llm.stage_router import StageRouter

    jid = uuid.UUID(job_id)
    vid = uuid.UUID(vertex_node_id)
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    stage_router: StageRouter = ctx["stage_router"]

    log = structlog.get_logger().bind(
        job_id=job_id, vertex_node_id=vertex_node_id, force=force
    )
    await set_tenant_from_job(session_factory, jid)
    set_job_from_arq(jid)
    log.info("node_summary_regeneration_started")

    async with session_factory() as session:
        orch = build_node_summary_orchestrator(session, stage_router)
        await orch.run(job_id=jid, vertex_node_id=vid, force=force)
        await session.commit()
    log.info("node_summary_regeneration_done")
