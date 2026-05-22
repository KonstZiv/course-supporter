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

import itertools
from pathlib import Path
from typing import TYPE_CHECKING

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.ingestion.video_pipeline import media
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    FrameDescription,
    FrameKind,
    SampledFrame,
    Scene,
    SttPause,
    SttResult,
    SttWord,
    VideoFileMetadata,
)

if TYPE_CHECKING:
    from course_supporter.models.source import SourceDocument
    from course_supporter.storage.orm import AuthoredDocument
    from course_supporter.stt.router import STTRouter

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


async def step_3_detection(stt: SttResult) -> list[Scene]:
    """Krok 3 — pre-LLM scene detection + frame sampling (§1).

    Skeleton: no ffmpeg / scenedetect (task 2.4.3). Emits one mock
    scene spanning the full duration with a single anchor frame.
    """
    return [
        Scene(
            scene_id=0,
            start_ms=0,
            end_ms=stt.duration_ms,
            frames=[
                SampledFrame(frame_position_ms=0, change_class=ChangeClass.FIRST),
            ],
        ),
    ]


async def step_4_pass1_vision(scenes: list[Scene]) -> list[FrameDescription]:
    """Krok 4 — Pass 1 / «Eyes» vision description (§1).

    Skeleton: no vision LLM / chunking (task 2.4.4). Emits one anchor
    description per sampled frame.
    """
    descriptions: list[FrameDescription] = []
    for scene in scenes:
        for frame in scene.frames:
            descriptions.append(
                FrameDescription(
                    scene_id=scene.scene_id,
                    frame_position_ms=frame.frame_position_ms,
                    description="Mock slide: title and bullet points.",
                    kind=FrameKind.ANCHOR,
                )
            )
    return descriptions


# ── Krok 5 — invoked from process_macro ────────────────────────────


async def step_5_pass2a_mapping(doc: SourceDocument) -> DocumentSummaryDraft:
    """Krok 5 — Pass 2a semantic mapping (§1).

    Skeleton: no premium text LLM (task 2.4.5). Builds a contiguous
    segment cover over the assembled reference text so the persist
    cascade (``DocumentSegmentRepository.create_batch`` bounds-check +
    ``DocumentSummaryDraft`` contiguity validator) is exercised on real
    data. The last segment is flagged ``noisy=True`` to drive the Pass
    2c branch in :func:`step_7_pass2c_cleanup`.
    """
    reference = doc.assemble_text()
    total = len(reference)
    segments: list[DocumentSegmentDraft] = []

    if total >= 2:
        split = total // 2
        segments.append(
            DocumentSegmentDraft(
                order=0,
                start_pos=0,
                end_pos=split,
                title="Intro (mock)",
                description="Mock opening segment.",
                main_concepts=["mock-concept-a"],
                secondary_concepts=[],
                noisy=False,
            )
        )
        segments.append(
            DocumentSegmentDraft(
                order=1,
                start_pos=split,
                end_pos=total,
                title="Body (mock, noisy)",
                description="Mock body segment flagged noisy.",
                main_concepts=["mock-concept-b"],
                secondary_concepts=["mock-concept-a"],
                noisy=True,
            )
        )
    elif total == 1:
        segments.append(
            DocumentSegmentDraft(
                order=0,
                start_pos=0,
                end_pos=1,
                title="Whole (mock)",
                description="Mock single segment.",
                main_concepts=["mock-concept-a"],
                secondary_concepts=[],
                noisy=True,
            )
        )
    # total == 0 → empty segments (the validator allows an empty cover).

    return DocumentSummaryDraft(
        title="Mock video summary",
        description="Skeleton mock summary (Phase 2.4 task 2.4.1).",
        main_concepts=["mock-concept-a", "mock-concept-b"],
        secondary_concepts=[],
        segments=segments,
    )


# ── Krok 6-7 — invoked from process_detail ─────────────────────────


async def step_6_pass2b_slice(
    doc: SourceDocument,
    summary_draft: DocumentSummaryDraft,
) -> list[DocumentSegmentDraft]:
    """Krok 6 — Pass 2b algorithmic slice (§1).

    Zero-LLM by design (task 2.4.6 finalises the transcript/visual merge
    format). Skeleton mirrors the audio Pass 2b slice: fill ``content``
    for each draft from ``doc.assemble_text()[start_pos:end_pos]`` when
    not already populated.
    """
    reference = doc.assemble_text()
    sliced: list[DocumentSegmentDraft] = []
    for draft in summary_draft.segments:
        if draft.content is not None:
            sliced.append(draft)
            continue
        raw_content = reference[draft.start_pos : draft.end_pos]
        sliced.append(draft.model_copy(update={"content": raw_content}))
    return sliced


async def step_7_pass2c_cleanup(
    segments: list[DocumentSegmentDraft],
) -> list[DocumentSegmentDraft]:
    """Krok 7 — Pass 2c cleanup of noisy segments (§1).

    Skeleton: no cheap text LLM (task 2.4.7). Selective on ``noisy``,
    mirroring the audio Pass 2c routing; the stub prefixes a marker
    instead of denoising. Non-noisy segments keep their raw slice.
    """
    cleaned: list[DocumentSegmentDraft] = []
    for draft in segments:
        if draft.noisy and draft.content is not None:
            cleaned.append(
                draft.model_copy(update={"content": f"[cleaned] {draft.content}"})
            )
        else:
            cleaned.append(draft)
    return cleaned
