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

from course_supporter.ingestion.factory import (
    create_heavy_steps,
    create_processors,
)
from course_supporter.language import (
    InvalidLanguageError,
    LanguageNotAllowedError,
    normalize_and_validate,
)
from course_supporter.models.source import SourceType
from course_supporter.service_logging import (
    set_job_from_arq,
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


async def arq_ingest_material(
    ctx: dict[str, Any],
    job_id: str,  # UUID as string (ARQ JSON serialization)
    material_id: str,  # UUID as string (ARQ JSON serialization)
    source_type: str,
    source_url: str,
    priority: str = "normal",
) -> None:
    """ARQ task: process a AuthoredDocument with job tracking.

    Thin orchestrator: validates priority, transitions to active,
    runs the processor, then delegates completion handling to
    :class:`~course_supporter.ingestion_callback.IngestionCallback`.

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

    detected_language: str | None = None

    async with session_factory() as session:
        job_repo = JobRepository(session)
        entry_repo = AuthoredDocumentRepository(session)
        node_repo = CourseNodeRepository(session)

        entry = await entry_repo.get_by_id(mid)
        if entry is None:
            log.error("material_entry_not_found", material_id=material_id)
            return

        # Resolve effective language: entry override → course default → None.
        # When None, STT will auto-detect and return detected_language which
        # we persist back to the entry after successful ingestion.
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
                doc = await processor.process_raw(resolved, router=router)

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
            submission_text = doc.safety_text()
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
            await session.commit()
            # ── /Pass 2b ──

            content = doc.model_dump_json()
            detected_language = doc.metadata.get("detected_language")

        except SecurityRejectedError as exc:
            # Stage 2 reject branch (KD-2.1-P, Phase 2.1 C6). The
            # ``safety_result`` row is already committed pre-raise so
            # rollback is a no-op for that row; callback handles the
            # cascade soft-delete in its fresh session via the
            # ``error_category`` discriminator.
            await session.rollback()
            await callback.on_failure(
                job_id=jid,
                material_id=mid,
                error_message=str(exc),
                error_category=exc.category,
            )
            log.warning(
                "ingestion_security_rejected",
                category=exc.category.value,
                detail=exc.detail,
            )
            return
        except Exception as exc:
            await session.rollback()
            await callback.on_failure(
                job_id=jid,
                material_id=mid,
                error_message=str(exc),
            )
            log.error("ingestion_failed", error=str(exc))
            return

    # Cache auto-detected language back to the entry for future STT calls.
    # Uses an atomic UPDATE ... WHERE language IS NULL to avoid a race
    # where a concurrent PATCH may set language between our check and write.
    #
    # STT output is a black-box external signal — a provider may return a
    # code outside the project whitelist (Task 2.4.13). Normalize through
    # the helper; if it cannot be resolved or is not allowed, warn-log and
    # skip the cache write (do NOT fail ingestion — language authority is
    # the course root, not STT auto-detect).
    if detected_language:
        try:
            normalized = normalize_and_validate(detected_language)
        except (InvalidLanguageError, LanguageNotAllowedError) as exc:
            log.warning(
                "language_auto_detect_skipped",
                raw=detected_language,
                reason=str(exc),
            )
        else:
            async with session_factory() as session:
                entry_repo = AuthoredDocumentRepository(session)
                updated = await entry_repo.set_language_if_unset(mid, normalized)
                await session.commit()
                if updated:
                    log.info(
                        "language_auto_detected_cached",
                        language=normalized,
                    )

    await callback.on_success(
        job_id=jid,
        material_id=mid,
        content_json=content,
    )
    log.info("ingestion_done")


async def arq_process_homework(
    ctx: dict[str, Any],
    job_id: str,
    submission_id: str,
) -> None:
    """ARQ task: process a homework submission.

    Orchestrates the full homework pipeline:
    safety check → task matching → Mentor review → webhook delivery.

    Args:
        ctx: ARQ worker context (session_factory, model_router, s3_client).
        job_id: Job UUID as string.
        submission_id: HomeworkSubmission UUID as string.
    """
    from course_supporter.homework.webhook import (
        build_reviewed_payload,
        deliver_webhook,
        resolve_webhook_url,
    )
    from course_supporter.security.archive import extract_submission_content
    from course_supporter.security.exceptions import SecurityRejectedError
    from course_supporter.security.schemas import (
        CourseContext,
        SecurityContext,
        Stage1RejectionResult,
    )
    from course_supporter.security.stage1 import run_stage1
    from course_supporter.security.stage2 import run_stage2_safety_check
    from course_supporter.storage.course_node_repository import (
        CourseNodeRepository,
    )
    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.job_repository import JobRepository
    from course_supporter.storage.student_repository import StudentRepository

    jid = uuid.UUID(job_id)
    sid = uuid.UUID(submission_id)

    from course_supporter.llm.stage_router import StageRouter

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    model_router: ModelRouter = ctx["model_router"]
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
        job_repo = JobRepository(session)
        hw_repo = HomeworkRepository(session)
        node_repo = CourseNodeRepository(session)
        try:
            # Load submission
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

            await job_repo.update_status(jid, "active")
            await hw_repo.update_status(sid, "safety_check")
            await session.commit()

            # --- HW-004: Safety check ---
            s3_key = s3.extract_key(submission.file_url)
            if s3_key is None:
                msg = f"Cannot extract S3 key from {submission.file_url}"
                raise ValueError(msg)

            file_path = await s3.download_file(s3_key)
            log.info("homework_file_downloaded", path=str(file_path))
            try:
                # Build course context for safety + matching
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
                # File already downloaded above; HOMEWORK_POLICY caps at 1 MB
                # so in-memory read is safe (per Phase 1.2 §6.2 option a ratify).
                file_bytes = file_path.read_bytes()
                try:
                    stage1_result = run_stage1(
                        filename=submission.original_filename or file_path.name,
                        content=file_bytes,
                        context="homework",
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
                    await job_repo.update_status(jid, "complete")
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
                    await job_repo.update_status(jid, "complete")
                    await session.commit()
                    log.warning(
                        "homework_rejected_safety",
                        reasoning=safety_result.reasoning,
                        violations=[v.value for v in safety_result.violations],
                    )
                    return

                # --- HW-006: Mentor review ---
                # HW-005 task matching block + HW-007a matched-notification
                # webhook removed in C9.3 (DD-2.1-AG). Phase 5 editable storage
                # ORM dropped; reroute on NodeSummaryFinal deferred to Phase 4.
                # ORM columns submission.task_hint_id + submission.matched_task_id
                # become orphan (writer dies, reader webhook payload always None);
                # drop_column deferred to natural cleanup cycle.
                await hw_repo.update_status(sid, "reviewing")
                await session.commit()

                from course_supporter.homework.mentor import MentorAgent
                from course_supporter.homework.mentor_context import (
                    build_mentor_context,
                )

                mentor_ctx = await build_mentor_context(
                    submission_content=content,
                    submission=submission,
                    session=session,
                )
                mentor = MentorAgent()
                review = await mentor.review(mentor_ctx, model_router)

                await hw_repo.store_review_result(
                    sid,
                    result=review.model_dump(mode="json"),
                    review_markdown=review.review_text,
                )
                # Update submission language + student preference
                from sqlalchemy import update as sa_update

                from course_supporter.storage.orm import (
                    HomeworkSubmission as HWModel,
                )
                from course_supporter.storage.orm import (
                    Student as StudentModel,
                )

                await session.execute(
                    sa_update(HWModel)
                    .where(HWModel.id == sid)
                    .values(response_language=review.response_language)
                )
                await session.execute(
                    sa_update(StudentModel)
                    .where(StudentModel.id == submission.student_id)
                    .values(preferred_language=review.response_language)
                )
                await session.commit()

                log.info(
                    "homework_reviewed",
                    passed=review.analysis.passed,
                    score=review.analysis.score,
                    language=review.response_language,
                    issues=len(review.analysis.issues),
                    notable=len(review.analysis.notable_solutions),
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

                await job_repo.update_status(jid, "complete")
                await session.commit()
                log.info("homework_processing_done")
            finally:
                if file_path.exists():
                    file_path.unlink()
                    log.debug("homework_temp_file_cleaned", path=str(file_path))

        except SecurityRejectedError as exc:
            exc.enrich(sec_ctx)
            await session.rollback()
            async with session_factory() as err_session:
                err_job_repo = JobRepository(err_session)
                err_hw_repo = HomeworkRepository(err_session)
                await err_job_repo.update_status(jid, "failed", error_message=str(exc))
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

        except Exception as exc:
            await session.rollback()
            async with session_factory() as err_session:
                err_job_repo = JobRepository(err_session)
                err_hw_repo = HomeworkRepository(err_session)
                await err_job_repo.update_status(jid, "failed", error_message=str(exc))
                try:
                    await err_hw_repo.update_status(
                        sid, "failed", error_message=str(exc)
                    )
                except ValueError as status_exc:
                    log.warning(
                        "homework_status_update_skipped",
                        reason=str(status_exc),
                    )
                await err_session.commit()
            log.error("homework_processing_failed", error=str(exc))
