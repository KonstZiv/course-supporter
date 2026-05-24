"""Seven pipeline gnízda (steps) of the canonical 7-step video flow.

Each function is one gnízdo of ``PHASE-2-4.md`` §1. Krok 1-2 are real
as of task 2.4.2 (ingestion via S3/yt-dlp + ffprobe; STT via ffmpeg
audio extraction → ElevenLabs Scribe core); Krok 3-7 remain offline
stubs returning mock §1 structures (tasks 2.4.3-2.4.7 fill them).

Data flows in-memory between steps (return values / arguments). The
real STT carrier is *additionally* written to Redis by the processor
(``video_stt_result:{job_id}``) for the future Pass 2a consumer — that
producer write lives in ``processor.py``, not here.

Filling order (per ``PHASE-2-4.md`` §3): each task replaces one or two
step bodies, leaving neighbours and the ``VideoProcessor`` orchestration
untouched. Real steps raise
:class:`~course_supporter.ingestion.base.UnsupportedFormatError`
(constraint) or :class:`~course_supporter.ingestion.base.ProcessingError`
(operational) per the per-task taxonomy (D3); the failure-injection seam
is to patch any step (or ``media.*``) to raise (see the tests).

Steps 1-4 are invoked from ``VideoProcessor.process_raw``; step 5 from
``process_macro``; steps 6-7 from ``process_detail``.
"""

from __future__ import annotations

import asyncio
import bisect
import itertools
import json
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from course_supporter.ingestion.audio import chars_per_word_cumsum
from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.schemas import (
    AudioPass2aResult,
    AudioPass2cResult,
    DocumentSegmentDraft,
    DocumentSummaryDraft,
    VisualSceneRef,
)
from course_supporter.ingestion.video_pipeline import detection, media, vision
from course_supporter.ingestion.video_pipeline.schemas import (
    STT_RESULT_KEY_TMPL,
    DetectionResult,
    FrameDescription,
    SttPause,
    SttResult,
    SttWord,
    VideoFileMetadata,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.models.source import ChunkType
from course_supporter.service_logging import get_current_job_id

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.models.source import SourceDocument
    from course_supporter.storage.orm import AuthoredDocument
    from course_supporter.stt.router import STTRouter

logger = structlog.get_logger()

# Video Pass 2a ladder stage (config/ladders_video.yaml). The "PASS" token
# trips ruff S105 (hardcoded-password heuristic) — it is a stage id, not a
# secret, hence the noqa (mirrors audio.py).
_PASS_2A_STAGE_NAME = "video_pass_2a_mapping"  # noqa: S105

# Video Pass 2c text-cleanup ladder stage (config/ladders_video.yaml). Separate
# calibration unit from audio Pass 2c (own ladder + prompt) per KD-2.3-X; same
# registered cheap-tier pool. noqa: S105 as above.
_PASS_2C_STAGE_NAME = "video_pass_2c_denoise"  # noqa: S105

# 150 min — mirrors AudioProcessor.MAX_DURATION_SEC (Phase 2.2 vision §5).
# Enforced worker-side after ffprobe, BEFORE STT, so an over-long video
# never reaches the (billable) Scribe call.
_MAX_DURATION_MS = 150 * 60 * 1000

# Minimum inter-word silence (ms) treated as a Pass 2a boundary candidate.
# rationale: starting default; calibrated in 2.4.5 (the real consumer of
# pauses — Pass 2a segment boundaries, KD2c).
_PAUSE_THRESHOLD_MS = 700


# ── Krok 1-4 — invoked from process_raw ────────────────────────────


async def step_1_ingest(
    source: AuthoredDocument,
    *,
    tmp: Path,
) -> tuple[Path, VideoFileMetadata]:
    """Krok 1 — resolve the local video + probe container metadata (§1).

    Two source paths (the S3 download already happened in the
    orchestrator's ``_resolve_s3_url``, so an ``s3://`` upload arrives as
    a local temp path here):

    * ``http(s)://`` → yt-dlp video-stream download into ``tmp``;
    * anything else → treated as an already-local file path.

    Then ffprobe fills ``file_metadata`` and the 150-min duration cap is
    enforced (constraint → ``UnsupportedFormatError``, R1 ``state=ERROR``)
    before any STT cost is incurred. Returns the resolved local path
    (consumed by Krok 2) alongside the metadata.
    """
    url = source.source_url
    if url.startswith(("http://", "https://")):
        video_path = await media.download_video(url, tmp)
    else:
        video_path = Path(url)

    file_metadata = await media.probe_metadata(video_path)

    if file_metadata.duration_ms > _MAX_DURATION_MS:
        raise UnsupportedFormatError(
            f"Video duration ({file_metadata.duration_ms / 60_000:.1f} min) "
            f"exceeds maximum {_MAX_DURATION_MS // 60_000} min."
        )

    return video_path, file_metadata


async def step_2_stt(
    video_path: Path,
    file_metadata: VideoFileMetadata,
    *,
    stt_router: STTRouter,
    tmp: Path,
) -> SttResult:
    """Krok 2 — extract the audio track and transcribe it (§1).

    ffmpeg extracts a Scribe-friendly mono 16 kHz mp3 into ``tmp`` (the
    processor-owned tempdir, so it is cleaned with the rest — the S3
    source path lives outside ``tmp`` and must not collect siblings).
    The file path is handed to the reused audio ElevenLabs Scribe core
    via ``STTRouter.transcribe`` (one external call). The STT-domain
    result (seconds) is converted to the §1 ms-based ``SttResult``;
    ``pauses`` are derived from inter-word silence (no provider field
    surfaces them). ``duration_ms`` comes from ffprobe (Krok 1), not STT
    word-ends.
    """
    audio_path = await media.extract_audio(video_path, tmp)

    # language=None mirrors AudioProcessor: always auto-detect; the
    # orchestrator caches the detected language back onto the entry.
    result = await stt_router.transcribe(
        "transcribe",
        str(audio_path),
        language=None,
    )

    words = [
        SttWord(
            text=w.text,
            start_ms=round(w.start_sec * 1000),
            end_ms=round(w.end_sec * 1000),
        )
        for w in result.words
    ]

    return SttResult(
        language=result.language or "und",
        duration_ms=file_metadata.duration_ms,
        words=words,
        pauses=_derive_pauses(words),
        detected_language=result.detected_language,
    )


def _derive_pauses(words: list[SttWord]) -> list[SttPause]:
    """Derive Pass 2a boundary candidates from inter-word silence (KD2c).

    Scribe surfaces no explicit pause field, so a pause is any gap
    ``>= _PAUSE_THRESHOLD_MS`` between consecutive words.
    """
    pauses: list[SttPause] = []
    for prev, nxt in itertools.pairwise(words):
        if nxt.start_ms - prev.end_ms >= _PAUSE_THRESHOLD_MS:
            pauses.append(SttPause(start_ms=prev.end_ms, end_ms=nxt.start_ms))
    return pauses


async def step_3_detection(
    video_path: Path,
    file_metadata: VideoFileMetadata,
    *,
    tmp: Path,
) -> DetectionResult:
    """Krok 3 — pre-LLM scene detection + frame sampling + dedup (§1).

    Delegates to :func:`detection.detect` (ffmpeg fps extraction → PiP
    detection → 5-metric tiered dedup → gap fill → 3-gate scene boundary).
    Zero LLM, deterministic. JPEG frames live in ``tmp`` (processor-owned;
    cleaned with the rest) and are referenced by ``SampledFrame.frame_path``
    for Pass 1. ffmpeg/cv2 failure → ``ProcessingError`` (raised by
    ``detection``/``media``); zero frames → ``ProcessingError`` (R1).
    """
    return await detection.detect(video_path, file_metadata, tmp)


async def step_4_pass1_vision(
    detection_result: DetectionResult,
    file_metadata: VideoFileMetadata,
    *,
    stage_router: StageRouter,
) -> list[FrameDescription]:
    """Krok 4 — Pass 1 / «Eyes» vision chain-diff description (§1).

    Delegates to :func:`vision.run_pass_1`: budget-driven chunking, chain-diff
    (anchor/diff per frame), PiP masking applied here, and chunk-response
    parsing into ``FrameDescription`` by the ``[FRAME HH:MM:SS]`` marker.
    Ladder ``video_pass_1_vision`` (Qwen3-VL → Gemini). Ladder exhaustion or
    a persistent chunk-parse mismatch → ``ProcessingError`` (R1).
    """
    return await vision.run_pass_1(
        detection_result, file_metadata, stage_router=stage_router
    )


# ── Krok 5 — invoked from process_macro ────────────────────────────


def _fmt_ts(position_ms: int) -> str:
    """``frame_position_ms`` → ``MM:SS`` (minutes may exceed 59 for long video)."""
    total_sec = position_ms // 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}"


def _build_visual_overlay(doc: SourceDocument, words: list[SttWord]) -> str:
    """Visual-overlay block (variant B): each frame anchored to a word index.

    A separate block (NOT inline in the word-stream) so the transcript
    word-index count stays untouched and the ``chars_per_word_cumsum`` bridge
    maps cleanly. The word-anchor is **deterministic** — a bisect of
    ``frame_position_ms`` into the word start times, not an LLM guess —
    giving the mapping LLM a reliable «this slide appears around word N» cue.
    ``frame_position_ms`` is read from the ``VISUAL_SCENE`` chunk ``metadata``
    (exact int ms; no float round-trip through ``start_sec``).
    """
    visual_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.VISUAL_SCENE]
    if not visual_chunks or not words:
        return ""
    word_starts = [w.start_ms for w in words]
    lines: list[str] = []
    for chunk in visual_chunks:
        fp_ms = int(chunk.metadata.get("frame_position_ms", 0))
        anchor = max(0, bisect.bisect_right(word_starts, fp_ms) - 1)
        lines.append(f"[word ~{anchor}, {_fmt_ts(fp_ms)}] {chunk.text}")
    return "\n".join(lines)


async def step_5_pass2a_mapping(
    doc: SourceDocument,
    *,
    redis: ArqRedis,
    stage_router: StageRouter,
) -> DocumentSummaryDraft:
    """Krok 5 — Pass 2a semantic mapping over the transcript word-stream (§1).

    Mirrors the audio Pass 2a word-idx contract (KD-2.2-A/F/H): reuses
    ``AudioPass2aResult`` (identical word-idx schema; the video-specific
    ``noisy`` semantics live in the prompt, not the schema) and
    ``chars_per_word_cumsum`` (the word-idx → char-offset bridge). Reads the
    STT word carrier from Redis (consumer side of the 2.4.2 producer write)
    and the Pass 1 visual descriptions from the ``VISUAL_SCENE`` chunks,
    anchoring each visual to a transcript word so the LLM segments the clean
    word-stream while seeing a deterministic visual-overlay block.

    Failures (carrier cache miss, ladder exhausted, persistent JSON
    validation fail, word-idx out of range) → ``ProcessingError`` (R1).
    """
    job_id = get_current_job_id()
    if job_id is None:
        raise ProcessingError(
            "step_5_pass2a_mapping requires ContextVar job_id; the ARQ task "
            "entry must set it before invocation."
        )

    raw = await redis.get(STT_RESULT_KEY_TMPL.format(job_id=job_id))
    if raw is None:
        raise ProcessingError(
            f"Video Pass 2a: STT carrier cache miss for job_id={job_id}. "
            f"process_raw (Krok 2) must populate video_stt_result before "
            f"process_macro runs."
        )
    stt = SttResult.model_validate_json(raw)
    words = stt.words
    total_word_count = len(words)

    words_json = json.dumps(
        [
            {"text": w.text, "start": w.start_ms / 1000.0, "end": w.end_ms / 1000.0}
            for w in words
        ],
        separators=(",", ":"),
    )
    visuals = _build_visual_overlay(doc, words)

    parsed: dict[str, AudioPass2aResult] = {}

    def _validator(content: str) -> None:
        """Translate a Pydantic ``ValidationError`` into a ``StructuralRetryError``.

        Reuses ``AudioPass2aResult`` (same word-idx schema) — the contiguity /
        full-cover / subsegment-cover validators apply unchanged, with
        ``total_word_count`` carried via the Pydantic context.
        """
        try:
            local = AudioPass2aResult.model_validate_json(
                content, context={"total_word_count": total_word_count}
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            first_loc = ".".join(str(x) for x in first.get("loc", []))
            logger.warning(
                "video_pass2a.validation.failed",
                job_id=str(job_id),
                first_error_msg=first.get("msg", ""),
                first_error_loc=first_loc,
                total_word_count=total_word_count,
            )
            feedback = (
                f"{first.get('msg', 'validation error')} "
                f"(field: {first_loc or '<root>'}). "
                "Regenerate the response with valid output."
            )
            raise StructuralRetryError(feedback) from exc
        parsed["result"] = local

    await stage_router.execute_for_stage(
        _PASS_2A_STAGE_NAME,
        response_validator=_validator,
        expects_json=True,
        words_count=total_word_count,
        words_json=words_json,
        visuals=visuals,
    )
    result = parsed["result"]

    cumsum = chars_per_word_cumsum(words)
    segment_drafts = [
        DocumentSegmentDraft(
            order=idx,
            start_pos=cumsum[seg.start_word_idx],
            end_pos=cumsum[seg.end_word_idx],
            title=seg.title,
            description=seg.description,
            main_concepts=list(seg.main_concepts),
            secondary_concepts=list(seg.secondary_concepts),
            start_time_sec=words[seg.start_word_idx].start_ms / 1000.0,
            end_time_sec=words[seg.end_word_idx - 1].end_ms / 1000.0,
            noisy=seg.noisy,
        )
        for idx, seg in enumerate(result.segments)
    ]

    all_main: set[str] = set()
    all_secondary: set[str] = set()
    for seg in result.segments:
        all_main.update(seg.main_concepts)
        all_secondary.update(seg.secondary_concepts)
    all_secondary -= all_main

    logger.debug(
        "video.pass2a.done",
        job_id=str(job_id),
        segments=len(segment_drafts),
        main_concepts=len(all_main),
        secondary_concepts=len(all_secondary),
        words=total_word_count,
        noisy=sum(1 for seg in result.segments if seg.noisy),
    )
    return DocumentSummaryDraft(
        title=result.title or "",
        description=result.description,
        main_concepts=sorted(all_main),
        secondary_concepts=sorted(all_secondary),
        segments=segment_drafts,
    )


# ── Krok 6-7 — invoked from process_detail ─────────────────────────


def _partition_visuals(
    doc: SourceDocument,
    segments: list[DocumentSegmentDraft],
) -> dict[int, list[VisualSceneRef]]:
    """Assign each ``VISUAL_SCENE`` chunk to exactly one segment (task 2.4.6).

    Partition (NOT interval membership) over the segment timeline by
    **segment start times**, half-open ``[start_i, start_{i+1})``:

    * a frame at ``fp`` belongs to segment ``i`` where
      ``start_i <= fp < start_{i+1}``;
    * frames before the first segment's start clamp to segment 0;
    * frames after the last start fall into the last segment.

    Why a partition and not ``start_time_sec <= fp <= end_time_sec``: Pass 2a
    sets ``end_time_sec = words[end_word_idx-1].end_ms/1000`` (exclusive word
    index), so adjacent segments are time-*disjoint* — an inter-word pause
    gapes between them. A slide change typically happens *during* that pause
    (the lecturer stops, clicks, then resumes), so an interval-membership
    filter would systematically drop the most synchronisation-relevant frames.
    A partition covers the whole axis with no gaps or overlaps, so every frame
    lands in exactly one segment. The bisect mirrors the word-anchor bisect in
    :func:`_build_visual_overlay`. Refs are kept in temporal (frame) order.

    Returns a ``{segment_index: [VisualSceneRef, ...]}`` map (every segment
    index present, possibly with an empty list).
    """
    result: dict[int, list[VisualSceneRef]] = {i: [] for i in range(len(segments))}
    visual_chunks = [c for c in doc.chunks if c.chunk_type == ChunkType.VISUAL_SCENE]
    starts = [seg.start_time_sec for seg in segments]
    if not visual_chunks or not segments or any(s is None for s in starts):
        # No visuals, no segments, or no timeline (non-video draft) — nothing
        # to partition; every segment keeps its empty list.
        return result
    start_secs: list[float] = [s for s in starts if s is not None]
    for chunk in sorted(
        visual_chunks,
        key=lambda c: int(c.metadata.get("frame_position_ms", 0)),
    ):
        fp_ms = int(chunk.metadata.get("frame_position_ms", 0))
        idx = max(0, bisect.bisect_right(start_secs, fp_ms / 1000.0) - 1)
        result[idx].append(
            VisualSceneRef(
                position_ms=fp_ms,
                description=chunk.text,
                kind=str(chunk.metadata.get("kind", "")),
                scene_id=int(chunk.metadata.get("scene_id", 0)),
            )
        )
    return result


async def step_6_pass2b_slice(
    doc: SourceDocument,
    summary_draft: DocumentSummaryDraft,
) -> list[DocumentSegmentDraft]:
    """Krok 6 — Pass 2b algorithmic slice + visual merge (§1).

    Zero-LLM, deterministic. Materialises the two time-aligned streams of a
    video segment (architectural direction (c), task 2.4.6):

    * ``content`` — transcript slice ``doc.assemble_text()[start_pos:end_pos]``
      (the audio/text Pass 2b precedent), filled when Pass 2a left it ``None``;
    * ``visual_content`` — the ``VISUAL_SCENE`` chunks whose frame timestamps
      partition into this segment's time range (see :func:`_partition_visuals`).

    Since this step runs only for video, every segment receives a
    ``visual_content`` list (possibly empty when no frame falls in its range).
    The two streams stay structurally separate so Pass 2c (task 2.4.7) cleans
    only the transcript ``content`` — the visual stream is clean by
    construction (KD2d).
    """
    reference = doc.assemble_text()
    visuals_by_idx = _partition_visuals(doc, summary_draft.segments)
    sliced: list[DocumentSegmentDraft] = []
    for idx, draft in enumerate(summary_draft.segments):
        update: dict[str, object] = {"visual_content": visuals_by_idx[idx]}
        if draft.content is None:
            update["content"] = reference[draft.start_pos : draft.end_pos]
        sliced.append(draft.model_copy(update=update))

    logger.debug(
        "video.pass2b.done",
        segments=len(sliced),
        visual_refs=sum(len(s.visual_content or []) for s in sliced),
        segments_with_visuals=sum(bool(s.visual_content) for s in sliced),
    )
    return sliced


async def _denoise_segment(
    draft: DocumentSegmentDraft,
    stage_router: StageRouter,
) -> str:
    """Pass 2c denoise call for a single noisy transcript segment (KD2d).

    Mirror of the audio Pass 2c precedent (``audio.py`` ``_denoise_segment``):
    the cheap text ladder (``video_pass_2c_denoise``) cleans the raw transcript
    slice (disfluencies / STT artefacts). The prompt commits to single-string
    output (no JSON wrapping); the ``response_validator`` closure wraps the raw
    response into ``AudioPass2cResult.content`` (reused — same single-string
    contract, KD-2.2-I) and a :class:`pydantic.ValidationError` is translated to
    :class:`StructuralRetryError` so the ladder retry + fallback runs before
    terminal failure. Only the transcript ``content`` is cleaned — the
    ``visual_content`` stream is clean by construction (KD2d) and never reaches
    here. Returns the cleaned content as a plain string.
    """
    raw_content = draft.content or ""
    job_id = get_current_job_id()
    parsed: dict[str, AudioPass2cResult] = {}

    def _video_pass_2c_validator(content: str) -> None:
        try:
            local = AudioPass2cResult.model_validate({"content": content})
        except ValidationError as exc:
            first = exc.errors()[0]
            first_loc = ".".join(str(x) for x in first.get("loc", []))
            logger.warning(
                "video_pass2c.validation.failed",
                job_id=str(job_id),
                segment_order=draft.order,
                first_error_msg=first.get("msg", ""),
                first_error_loc=first_loc,
            )
            feedback = (
                f"{first.get('msg', 'validation error')} "
                f"(field: {first_loc or '<root>'}). "
                "Regenerate the response as plain cleaned text."
            )
            raise StructuralRetryError(feedback) from exc
        parsed["result"] = local

    await stage_router.execute_for_stage(
        _PASS_2C_STAGE_NAME,
        response_validator=_video_pass_2c_validator,
        content=raw_content,
        title=draft.title,
        description=draft.description,
        main_concepts_json=json.dumps(draft.main_concepts, separators=(",", ":")),
        secondary_concepts_json=json.dumps(
            draft.secondary_concepts, separators=(",", ":")
        ),
    )
    return parsed["result"].content


async def step_7_pass2c_cleanup(
    segments: list[DocumentSegmentDraft],
    *,
    stage_router: StageRouter,
) -> list[DocumentSegmentDraft]:
    """Krok 7 — Pass 2c selective denoise of noisy transcript segments (§1).

    The cheap text ladder (``video_pass_2c_denoise``) cleans the transcript
    ``content`` of segments Pass 2a flagged ``noisy=True`` (disfluencies, STT
    artefacts); non-noisy segments keep their raw slice. Mirrors the audio
    Pass 2c routing — ``asyncio.gather`` over the noisy subset (chunks
    independent). **Only ``content`` is touched**: ``visual_content`` is clean
    by construction (KD2d) and passes through ``model_copy`` untouched.

    An empty noisy set returns the list as-is (zero LLM calls). The
    ``content`` guard mirrors audio (a ``noisy`` segment whose ``content`` is
    falsy is skipped — it cannot occur after Pass 2b but is guarded). Ladder
    exhaustion propagates from ``execute_for_stage`` as ``ProcessingError``
    (R1) up through ``process_detail``.
    """
    noisy_indices = [i for i, d in enumerate(segments) if d.noisy and d.content]
    logger.debug(
        "video.pass2c.selected",
        noisy_orders=[segments[i].order for i in noisy_indices],
        count=len(noisy_indices),
        total_segments=len(segments),
    )
    if not noisy_indices:
        return segments

    cleaned_contents = await asyncio.gather(
        *(_denoise_segment(segments[i], stage_router) for i in noisy_indices)
    )
    result = list(segments)
    for i, cleaned in zip(noisy_indices, cleaned_contents, strict=True):
        result[i] = result[i].model_copy(update={"content": cleaned})
    logger.debug("video.pass2c.done", denoised=len(noisy_indices))
    return result
