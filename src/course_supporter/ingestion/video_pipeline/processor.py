"""VideoProcessor — 7-step video pipeline (Phase 2.4).

The canonical 7-step flow (``PHASE-2-4.md`` §1) maps onto the existing
3-method :class:`~course_supporter.ingestion.base.MaterialProcessor`
contract without extending it (symmetric with the audio precedent, which
folds Pass 2c into ``process_detail``):

* :meth:`process_raw`    — Krok 1-4 (ingest → STT → detection → Pass 1)
                           → ``SourceDocument``.
* :meth:`process_macro`  — Krok 5 (Pass 2a mapping) → ``DocumentSummaryDraft``.
* :meth:`process_detail` — Krok 6-7 (Pass 2b slice + Pass 2c cleanup)
                           → ``list[DocumentSegmentDraft]``.

Each gnízdo lives in :mod:`course_supporter.ingestion.video_pipeline.steps`.
Krok 1-2 are real as of task 2.4.2 (ingestion + STT); Krok 3-7 remain
stubs (tasks 2.4.3-2.4.7). Data flows in-memory between steps; the STT
carrier is *additionally* written to Redis (``video_stt_result:{job_id}``,
TTL = ``worker_job_timeout``) as the producer side of the inter-stage
transport for the future Pass 2a consumer (Krok 5, task 2.4.5) — mirroring
the audio
word-cache, NOT the deadlocking ``jobs`` row (rule #12). The namespace
never imported the ``course_supporter.vd`` module (isolation per task 2.4.1
acceptance #3; ``vd/`` itself was removed in task 2.4.9A).

Factory dispatch invariant: ``create_processors`` routes by
``source_type`` so this processor only receives ``SourceType.VIDEO``
inputs; no entry guard is needed (matches AudioProcessor / Phase 2.2).
``stt_router`` + ``redis`` + ``stage_router`` are required constructor
deps (STT in Krok 2, the Redis carrier, and the Pass 1 vision ladder in
Krok 4) — the factory registers VIDEO only when all three are present.
``stage_router`` is the VIDEO-only extra over AUDIO: Pass 1 lives in
``process_raw`` (Stage 2 safety reads ``doc.assemble_text()`` built from
the frame descriptions), so the vision ladder cannot arrive via the
``process_macro`` method argument and is injected here instead
(direct-injection, the video step of DD-2.3-AF).
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from course_supporter.config import get_settings
from course_supporter.ingestion.base import MaterialProcessor, ProcessingError
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.ingestion.video_pipeline.schemas import STT_RESULT_KEY_TMPL
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.service_logging import get_current_job_id

if TYPE_CHECKING:
    import uuid

    from arq.connections import ArqRedis

    from course_supporter.ingestion.schemas import (
        DocumentSegmentDraft,
        DocumentSummaryDraft,
    )
    from course_supporter.ingestion.video_pipeline.schemas import (
        FrameDescription,
        SttResult,
    )
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument
    from course_supporter.stt.router import STTRouter

logger = structlog.get_logger()


class VideoProcessor(MaterialProcessor):
    """Video source-type processor — three-stage ingestion over 7 gnízda.

    See module docstring for the 7-step → 3-method mapping, the Redis
    inter-stage carrier, and the asymmetry vs ``AudioProcessor``. Requires
    ``stt_router`` (Krok 2 transcription), ``redis`` (STT-carrier write),
    and ``stage_router`` (Krok 4 Pass 1 vision ladder).
    """

    def __init__(
        self,
        *,
        stt_router: STTRouter,
        redis: ArqRedis,
        stage_router: StageRouter,
    ) -> None:
        super().__init__()
        self._stt_router = stt_router
        self._redis = redis
        self._stage_router = stage_router

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Krok 1-4 → ``SourceDocument``.

        Reads ``job_id`` from the ContextVar (set at ARQ task entry) for
        the Redis carrier key. All transient files (yt-dlp download,
        extracted audio, sampled frame JPEGs) live in a processor-owned
        tempdir cleaned on exit. The ``router`` (``ModelRouter``) parameter
        is accepted for ABC symmetry but unused; Pass 1 (Krok 4) uses the
        injected ``self._stage_router`` (the vision ladder), not this arg.
        """
        job_id = get_current_job_id()
        if job_id is None:
            raise ProcessingError(
                "VideoProcessor.process_raw requires ContextVar job_id; "
                "the ARQ task entry must set it before invocation."
            )

        raw_start = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="video_ingest_") as tmp_dir:
            tmp = Path(tmp_dir)

            step_start = time.perf_counter()
            video_path, file_metadata = await steps.step_1_ingest(source, tmp=tmp)
            logger.debug(
                "video.step_1_ingest.done",
                elapsed_ms=int((time.perf_counter() - step_start) * 1000),
                duration_ms=file_metadata.duration_ms,
            )

            step_start = time.perf_counter()
            stt = await steps.step_2_stt(
                video_path,
                file_metadata,
                stt_router=self._stt_router,
                tmp=tmp,
            )
            logger.debug(
                "video.step_2_stt.done",
                elapsed_ms=int((time.perf_counter() - step_start) * 1000),
                words=len(stt.words),
                pauses=len(stt.pauses),
                language=stt.language,
                detected_language=stt.detected_language,
            )

            await self._store_stt_result(job_id, stt)

            step_start = time.perf_counter()
            detection_result = await steps.step_3_detection(
                video_path, file_metadata, tmp=tmp
            )
            logger.debug(
                "video.step_3_detection.done",
                elapsed_ms=int((time.perf_counter() - step_start) * 1000),
                scenes=len(detection_result.scenes),
                pip=detection_result.pip_mask is not None,
            )

            step_start = time.perf_counter()
            frame_descriptions = await steps.step_4_pass1_vision(
                detection_result, file_metadata, stage_router=self._stage_router
            )
            logger.debug(
                "video.step_4_pass1_vision.done",
                elapsed_ms=int((time.perf_counter() - step_start) * 1000),
                frame_descriptions=len(frame_descriptions),
            )

            doc = self._assemble_source_document(source, stt, frame_descriptions)

        if stt.detected_language:
            doc.metadata["detected_language"] = stt.detected_language

        logger.debug(
            "video.process_raw.done",
            elapsed_ms=int((time.perf_counter() - raw_start) * 1000),
            transcript_words=len(stt.words),
            visual_scenes=len(frame_descriptions),
            detected_language=stt.detected_language,
        )
        return doc

    async def _store_stt_result(self, job_id: uuid.UUID, stt: SttResult) -> None:
        """Write the STT carrier to Redis for the future Pass 2a consumer.

        Producer side of the inter-stage transport (task 2.4.2); the
        consumer (Krok 5, Pass 2a) lands in task 2.4.5. Key
        ``video_stt_result:{job_id}``, payload ``SttResult.model_dump_json()``
        — the audio word-cache pattern, NOT the ``jobs`` row (rule #12
        deadlock).

        TTL is bound to ``worker_job_timeout`` (DD-3.3c-G): the carrier
        must outlive the whole job because Pass 2a reads near job end,
        after the duration-proportional frame-decode (3.3c-B). The job
        ceiling ``>=`` any internal write→read span, so the survival
        invariant holds by construction without a per-stage CPU estimate.
        """
        key = STT_RESULT_KEY_TMPL.format(job_id=job_id)
        ttl = get_settings().worker_job_timeout
        await self._redis.set(key, stt.model_dump_json(), ex=ttl)

    @staticmethod
    def _assemble_source_document(
        source: AuthoredDocument,
        stt: SttResult,
        frame_descriptions: list[FrameDescription],
    ) -> SourceDocument:
        """Build the canonical ``SourceDocument`` from Krok 2 + Krok 4 output.

        Transcript words join into a single ``TRANSCRIPT`` chunk; each Pass 1
        frame description becomes a ``VISUAL_SCENE`` chunk carrying the full
        ``FrameDescription`` fidelity in ``metadata``
        (``frame_position_ms`` / ``kind`` / ``scene_id``) — task 2.4.5 reads
        Pass 1 output back from these chunks (no separate instance-state
        carry), so the chunks are the single source of the visual content.

        Two reference surfaces follow from this shape (task 2.4.5):
        ``assemble_text()`` excludes the ``VISUAL_SCENE`` chunks and returns
        the transcript word-stream (the Pass 2a/2b mapping/slice bridge);
        ``safety_text()`` keeps every chunk (the Stage 2 safety surface). The
        ``assemble_text == " ".join(words)`` invariant is asserted here as the
        runtime precondition for the ``chars_per_word_cumsum`` char-offset
        bridge (KD-2.2-E precedent, ``audio.py``).
        """
        chunks: list[ContentChunk] = []
        index = 0

        transcript_text = " ".join(word.text for word in stt.words)
        if transcript_text:
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text=transcript_text,
                    index=index,
                    start_sec=(stt.words[0].start_ms / 1000.0 if stt.words else None),
                    end_sec=(stt.words[-1].end_ms / 1000.0 if stt.words else None),
                )
            )
            index += 1

        for frame in frame_descriptions:
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.VISUAL_SCENE,
                    text=frame.description,
                    index=index,
                    start_sec=frame.frame_position_ms / 1000.0,
                    metadata={
                        "frame_position_ms": frame.frame_position_ms,
                        "kind": frame.kind.value,
                        "scene_id": frame.scene_id,
                    },
                )
            )
            index += 1

        doc = SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.filename or "",
            chunks=chunks,
        )

        actual = doc.assemble_text()
        assert actual == transcript_text, (
            f"VideoProcessor invariant violated: assemble_text() "
            f"(len={len(actual)}) != ' '.join(words) (len={len(transcript_text)}). "
            f"assemble_text must exclude VISUAL_SCENE and space-join the "
            f"transcript word-stream so the chars_per_word_cumsum bridge maps."
        )
        return doc

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Krok 5 — Pass 2a semantic mapping (word-idx, task 2.4.5).

        Reads the STT word carrier from Redis (consumer side of the 2.4.2
        producer write) and the Pass 1 visual descriptions from the
        ``VISUAL_SCENE`` chunks, then maps the transcript word-stream into a
        ``DocumentSummaryDraft`` via the ``video_pass_2a_mapping`` ladder
        (text-reasoning). ``router`` is the StageRouter passed by the
        orchestrator (method-arg, as audio / presentation), distinct from the
        ``self._stage_router`` the Krok 4 vision Pass 1 uses.
        """
        macro_start = time.perf_counter()
        summary_draft = await steps.step_5_pass2a_mapping(
            doc, redis=self._redis, stage_router=router
        )
        logger.debug(
            "video.process_macro.done",
            elapsed_ms=int((time.perf_counter() - macro_start) * 1000),
            segments=len(summary_draft.segments),
        )
        return summary_draft

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
        *,
        router: StageRouter | None = None,
    ) -> list[DocumentSegmentDraft]:
        """Krok 6-7 — Pass 2b slice + Pass 2c selective denoise (task 2.4.7).

        Pass 2b (``step_6``) is zero-LLM. Pass 2c (``step_7``) routes the
        transcript ``content`` of ``noisy`` segments through the
        ``video_pass_2c_denoise`` ladder. The router is the ctor-injected
        ``self._stage_router`` — **not** the keyword-only ``router`` arg: the
        orchestrator passes a router only to ``process_macro`` (``api/tasks.py``),
        never to ``process_detail``, so Pass 2c sources the same StageRouter
        that Krok 4 Pass 1 already uses (``self._stage_router``). VideoProcessor
        has it from the ctor (task 2.4.4), so Pass 2c runs without any
        orchestrator/ABC change. The ``router`` kwarg is kept for ABC symmetry
        with AudioProcessor and is otherwise unused here.
        """
        detail_start = time.perf_counter()
        sliced = await steps.step_6_pass2b_slice(doc, summary_draft)
        result = await steps.step_7_pass2c_cleanup(
            sliced, stage_router=self._stage_router
        )
        logger.debug(
            "video.process_detail.done",
            elapsed_ms=int((time.perf_counter() - detail_start) * 1000),
            segments=len(result),
        )
        return result
