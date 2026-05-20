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
* ``SourceType.VIDEO`` keeps the ``"\\n\\n"`` default; transcript +
  visual-scene chunks remain visually separable for downstream stages.
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
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    WEB_CONTENT = "web_content"
    VISUAL_SCENE = "visual_scene"


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

    def assemble_text(self) -> str:
        """Canonical reference text used for LLM offset semantics.

        Pass 2a routes this exact string through the mapping prompt; the
        emitted ``start_pos`` / ``end_pos`` are inclusive/exclusive char
        offsets into it. Pass 2b slices this same string to materialise
        ``DocumentSegment.content``, and Stage 2 LLM safety check sees the
        same body. Centralising the assembly here prevents silent offset
        drift between the three pipeline stages.

        Separator is source_type-conditional (KD-2.2-E): audio transcripts
        join word/segment text with a single space because STT output is
        already whitespace-tokenised continuous speech; other source types
        keep the paragraph-style "\\n\\n" separator preserving authored
        chunk boundaries (slides, paragraphs, scenes).
        """
        separator = " " if self.source_type == SourceType.AUDIO else "\n\n"
        return separator.join(chunk.text for chunk in self.chunks if chunk.text)
