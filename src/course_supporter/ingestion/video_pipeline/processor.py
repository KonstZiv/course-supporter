"""VideoProcessor skeleton — 7-step video pipeline on stubs (Phase 2.4).

Task 2.4.1: a thin, end-to-end topology kistyak. The canonical 7-step
flow (``PHASE-2-4.md`` §1) maps onto the existing 3-method
:class:`~course_supporter.ingestion.base.MaterialProcessor` contract
without extending it (symmetric with the audio precedent, which folds
Pass 2c into ``process_detail``):

* :meth:`process_raw`    — Krok 1-4 (ingest → STT → detection → Pass 1)
                           → ``SourceDocument``.
* :meth:`process_macro`  — Krok 5 (Pass 2a mapping) → ``DocumentSummaryDraft``.
* :meth:`process_detail` — Krok 6-7 (Pass 2b slice + Pass 2c cleanup)
                           → ``list[DocumentSegmentDraft]``.

Each gnízdo lives in :mod:`course_supporter.ingestion.video_pipeline.steps`
as an offline stub; data flows in-memory between them. No external calls
(S3, ffmpeg, ElevenLabs, LLM) and no ``Job.stage_progress`` / Redis are
touched in the skeleton — real edges + the inter-stage transport land in
tasks 2.4.2-2.4.7. The new namespace has zero imports from the legacy
``course_supporter.vd`` module (isolation per task 2.4.1 acceptance #3).

Factory dispatch invariant: ``create_processors`` routes by
``source_type`` so this processor only receives ``SourceType.VIDEO``
inputs; no entry guard is needed (matches AudioProcessor / Phase 2.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.ingestion.base import MaterialProcessor
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
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


class VideoProcessor(MaterialProcessor):
    """Video source-type processor — skeleton over 7 stub gnízda.

    See module docstring for the 7-step → 3-method mapping. Takes no
    constructor dependencies: the skeleton makes zero external calls, so
    STT / vision / LLM clients are wired only when their real gnízda land
    (tasks 2.4.2/2.4.4/2.4.5/2.4.7).
    """

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Krok 1-4 → ``SourceDocument``.

        The ``router`` parameter is accepted for ABC symmetry but unused
        in the skeleton (the real Pass 1 vision call wires its own ladder
        in task 2.4.4).
        """
        file_metadata = await steps.step_1_ingest(source)
        stt = await steps.step_2_stt(source, file_metadata)
        scenes = await steps.step_3_detection(stt)
        frame_descriptions = await steps.step_4_pass1_vision(scenes)
        return self._assemble_source_document(source, stt, frame_descriptions)

    @staticmethod
    def _assemble_source_document(
        source: AuthoredDocument,
        stt: SttResult,
        frame_descriptions: list[FrameDescription],
    ) -> SourceDocument:
        """Build the canonical ``SourceDocument`` from Krok 2 + Krok 4 output.

        Transcript words join into a single ``TRANSCRIPT`` chunk; each
        Pass 1 frame description becomes a ``VISUAL_SCENE`` chunk.
        ``assemble_text`` keeps the ``"\\n\\n"`` separator for video
        (``models/source.py`` KD-2.2-E), so transcript and visual blocks
        stay separable in the reference text that Pass 2a/2b operate on.
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
                )
            )
            index += 1

        return SourceDocument(
            source_type=SourceType.VIDEO,
            source_url=source.source_url,
            title=source.filename or "",
            chunks=chunks,
        )

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Krok 5 — Pass 2a mapping (stub).

        The ``router`` parameter is accepted to match the ABC / orchestrator
        call site but unused in the skeleton (real Pass 2a wires the text
        ladder in task 2.4.5).
        """
        return await steps.step_5_pass2a_mapping(doc)

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
        *,
        router: StageRouter | None = None,
    ) -> list[DocumentSegmentDraft]:
        """Krok 6-7 — Pass 2b slice + Pass 2c cleanup (stubs).

        Extends the ABC ``process_detail`` signature with the keyword-only
        ``router`` (symmetric with AudioProcessor). The orchestrator calls
        this without a router; the skeleton's Pass 2c stub needs none (real
        cleanup wires the cheap text ladder in task 2.4.7).
        """
        sliced = await steps.step_6_pass2b_slice(doc, summary_draft)
        return await steps.step_7_pass2c_cleanup(sliced)
