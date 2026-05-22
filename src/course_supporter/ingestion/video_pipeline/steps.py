"""Seven pipeline gnízda (steps) as offline stubs (Phase 2.4 task 2.4.1).

Each function is one gnízdo of the canonical 7-step video flow
(``PHASE-2-4.md`` §1). In the skeleton every step is a deterministic
stub: zero network / disk / LLM, returns a mock structure of the §1
shape. Data flows in-memory between steps (return values / arguments);
no ``Job.stage_progress`` or Redis is written in the skeleton.

Filling order (per ``PHASE-2-4.md`` §3): each later task (2.4.2-2.4.7)
replaces the body of exactly one step here with real logic, without
touching its neighbours. Real implementations raise
:class:`~course_supporter.ingestion.base.ProcessingError` per their
own error taxonomy (drafted per-task); the skeleton's failure-injection
point is to patch any one of these step functions to raise (see
``tests/integration/test_video_skeleton.py``).

Steps 1-4 are invoked from ``VideoProcessor.process_raw``; step 5 from
``process_macro``; steps 6-7 from ``process_detail``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
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


# ── Krok 1-4 — invoked from process_raw ────────────────────────────


async def step_1_ingest(source: AuthoredDocument) -> VideoFileMetadata:
    """Krok 1 — resolve container metadata (§1).

    Skeleton: no S3 download / ffprobe (task 2.4.2). Reads
    ``source_url`` only to mirror the real signature; never opens the
    file. Returns a fixed mock metadata shape.
    """
    _ = source.source_url
    return VideoFileMetadata(
        duration_ms=600_000,
        codec="h264",
        resolution="1920x1080",
    )


async def step_2_stt(
    source: AuthoredDocument,
    file_metadata: VideoFileMetadata,
) -> SttResult:
    """Krok 2 — speech-to-text (§1).

    Skeleton: no audio extraction / ElevenLabs call (task 2.4.2).
    Returns a tiny deterministic word stream plus one pause candidate.
    """
    _ = source.source_url
    return SttResult(
        language="en",
        duration_ms=file_metadata.duration_ms,
        words=[
            SttWord(text="Hello", start_ms=0, end_ms=500),
            SttWord(text="world", start_ms=500, end_ms=1000),
        ],
        pauses=[SttPause(start_ms=1000, end_ms=1500)],
    )


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
