"""Source material schemas for ingestion pipeline."""

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


class MaterialRole(StrEnum):
    """Role of a material in the course. Mirrors ORM material_role_enum."""

    EDUCATIONAL = "educational"
    METHODOLOGICAL = "methodological"


class ChunkType(StrEnum):
    """Types of content chunks produced by processors."""

    TRANSCRIPT = "transcript"
    SLIDE_TEXT = "slide_text"
    SLIDE_DESCRIPTION = "slide_description"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    WEB_CONTENT = "web_content"
    METADATA = "metadata"
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
