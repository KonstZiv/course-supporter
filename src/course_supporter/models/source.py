"""Source material schemas for ingestion pipeline.

``SourceDocument.assemble_text`` joins per-chunk text with a
source_type-conditional separator (KD-2.2-E baseline, KD-2.3-L
presentation carry-forward):

* ``SourceType.AUDIO`` uses a single space — STT output arrives
  pre-tokenised as a continuous word stream, so any wider separator
  would inject phantom whitespace into the reference text seen by the
  LLM (Pass 2a) and by the segment-content slice (Pass 2b).
* ``SourceType.TEXT`` and ``SourceType.WEB`` use ``"\\n\\n"`` to
  preserve authored paragraph boundaries — the authored chunk shape
  itself encodes structural intent.
* ``SourceType.PRESENTATION`` uses ``"\\n\\n"`` so slide-level text
  blocks remain visually separable in the assembled reference text and
  align with the Pass 2a structural-segment contract (one segment may
  span multiple slides; segment boundaries are slide-aligned via the
  ``chars_per_slide_cumsum`` bridge in
  :mod:`course_supporter.ingestion.presentation`).
* ``SourceType.VIDEO`` joins the **transcript word-stream only** with a
  single space (audio-like word-idx mapping, Phase 2.4 task 2.4.5):
  ``VISUAL_SCENE`` chunks are excluded from this mapping/slice reference
  so the ``chars_per_word_cumsum`` bridge maps cleanly. The visual-scene
  descriptions stay in ``chunks`` and reach the Stage 2 safety check via
  :meth:`SourceDocument.safety_text` instead.

Two reference surfaces (Phase 2.4 task 2.4.5): :meth:`assemble_text` is
the **mapping/slice** reference (Pass 2a offsets, Pass 2b slice);
:meth:`safety_text` is the **full authored surface** the Stage 2 LLM
safety check sees. They coincide for every single-stream source type;
only video (transcript + visual) splits them.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    """Types of source materials. Mirrors ORM source_type_enum."""

    VIDEO = "video"
    PRESENTATION = "presentation"
    TEXT = "text"
    WEB = "web"
    AUDIO = "audio"
    CODE = "code"


class MaterialRole(StrEnum):
    """Role of a material in the course. Mirrors ORM material_role_enum."""

    EDUCATIONAL = "educational"
    METHODOLOGICAL = "methodological"


class AssignmentType(StrEnum):
    """Assignment types with increasing complexity and duration.

    Translocated from models/methodist.py in C9.0 (K-stop-1 resolution):
    AuthoredDocument.task_type is a core document taxonomy attribute,
    not a methodist-internal concept. Methodist legacy layer is deleted
    in C9.3 (Phase 5 + methodist cleanup).
    """

    TEST = "test"
    SHORT_TASK = "short_task"
    TASK = "task"
    PROJECT = "project"


class ChunkType(StrEnum):
    """Types of content chunks produced by processors."""

    TRANSCRIPT = "transcript"
    SLIDE_TEXT = "slide_text"
    # Pass 1 visual description of an image-only presentation slide, inserted
    # into doc.chunks so its text becomes segment content (road (a)). Distinct
    # from VISUAL_SCENE, which is the video source_type's second stream.
    SLIDE_VISUAL = "slide_visual"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    WEB_CONTENT = "web_content"
    VISUAL_SCENE = "visual_scene"
    CODE_FILE = "code_file"


class ContentChunk(BaseModel):
    """Single chunk of extracted content.

    Each processor produces a list of these. The chunk_type identifies
    the source (transcript, slide text, etc.) and metadata carries
    type-specific details (slide numbers, heading levels, scene info).
    """

    chunk_type: ChunkType
    text: str
    index: int = 0
    start_sec: float | None = Field(
        default=None,
        description="Start timestamp in seconds (video/audio chunks).",
    )
    end_sec: float | None = Field(
        default=None,
        description="End timestamp in seconds (video/audio chunks).",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceDocument(BaseModel):
    """Unified output of any MaterialProcessor.

    Contains all extracted content from a single source material
    (one video, one PDF, etc.) as a list of ContentChunks.
    """

    source_type: SourceType
    source_url: str
    title: str = ""
    chunks: list[ContentChunk] = Field(default_factory=list)
    processed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
    language: str | None = Field(
        default=None,
        description=(
            "Canonical ISO 639-3 code of the course language (NOT the "
            "detected language of the input). Set by the orchestrator "
            "after ``process_raw`` and before ``process_macro`` so the "
            "Pass 2a render-context can pin the output language (task "
            "2.4.14). ``None`` is a defensive unrooted-case sentinel — "
            "post task-2.4.13 (course default_language mandatory at "
            "root) no rooted course should reach Pass 2a with "
            "``language=None``; the Pass 2a prompts treat ``None`` as a "
            "fallback to legacy «detect from input» behaviour."
        ),
    )

    def assemble_text(self) -> str:
        """Mapping/slice reference text for LLM offset semantics.

        Pass 2a routes this exact string through the mapping prompt; the
        emitted ``start_pos`` / ``end_pos`` are inclusive/exclusive char
        offsets into it. Pass 2b slices this same string to materialise
        ``DocumentSegment.content``. Centralising the assembly here prevents
        silent offset drift between those two stages.

        The Stage 2 LLM safety check uses :meth:`safety_text`, not this
        method — they coincide for single-stream source types but diverge
        for video (see below), where this reference narrows to the
        transcript while safety still sees the full surface.

        Separator is source_type-conditional (KD-2.2-E):

        * audio / video transcripts join with a single space — STT output
          is already whitespace-tokenised continuous speech, so the
          word-idx bridge (``chars_per_word_cumsum``) expects exactly that;
        * video additionally excludes ``VISUAL_SCENE`` chunks (task 2.4.5)
          so the bridge maps over the transcript word-stream alone;
        * text / web / presentation keep the paragraph-style "\\n\\n"
          separator preserving authored chunk boundaries;
        * code (task-code-materials F1) also joins with "\\n\\n" — but the
          branch is EXPLICIT, not a fall-through: each ``CODE_FILE``
          chunk's text is ``path-header + raw file body`` and the
          CodeProcessor computes segment offsets deterministically over
          this exact join (offsets are never LLM-emitted for code), so
          the separator here is load-bearing offset arithmetic.
        """
        if self.source_type == SourceType.VIDEO:
            return " ".join(
                chunk.text
                for chunk in self.chunks
                if chunk.text and chunk.chunk_type != ChunkType.VISUAL_SCENE
            )
        if self.source_type == SourceType.CODE:
            return "\n\n".join(chunk.text for chunk in self.chunks if chunk.text)
        separator = " " if self.source_type == SourceType.AUDIO else "\n\n"
        return separator.join(chunk.text for chunk in self.chunks if chunk.text)

    def safety_text(self) -> str:
        """Full authored surface for the Stage 2 LLM safety check (KD-2.1-P).

        Distinct from :meth:`assemble_text`, which for video narrows to the
        transcript word-stream to serve the word-idx mapping/slice bridge.
        Stage 2 must still see every authored byte — including the
        visual-scene descriptions that reproduce on-screen slide text/code —
        so this method returns the complete surface.

        Default ``== assemble_text()``: audio / text / web / presentation /
        code are single-stream, so their safety surface already equals the
        mapping reference (zero behaviour change). For code this IS the
        ratified F6 contract: safety sees the raw typicality-filtered full
        text — never a skeleton (defense-in-depth); the byte backstop and
        the ladder ``input_budget_ratio`` live at the safety call site and
        stage config, not here. The video override is byte-identical to
        the pre-task-2.4.5 ``assemble_text`` (a ``"\\n\\n"`` join of ALL
        chunks, transcript + visual), so the Stage 2 safety surface is
        unchanged by the 2.4.5 reference rework.
        """
        if self.source_type == SourceType.VIDEO:
            return "\n\n".join(chunk.text for chunk in self.chunks if chunk.text)
        return self.assemble_text()
