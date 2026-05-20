"""Heavy step protocols and param/result models.

Defines typed contracts for all heavy (serverless-ready) operations:
- Transcription (Whisper / Gemini)
- Web scraping (trafilatura)

Each heavy step is a plain async callable with a clean contract:
structured params in → structured result out. No DB, no S3, no ORM.
Processors become orchestrators that call these functions via DI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Transcription (Whisper local / Gemini API / future Lambda)
# ---------------------------------------------------------------------------


class WhisperModelSize(StrEnum):
    """Available Whisper model sizes."""

    TINY = "tiny"
    BASE = "base"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TranscribeParams(BaseModel):
    """Parameters for audio transcription."""

    model_name: WhisperModelSize = Field(
        default=WhisperModelSize.BASE,
        description="Whisper model size.",
    )
    language: str | None = Field(
        default=None,
        description="ISO 639-1 language code. None = auto-detect.",
        pattern=r"^[a-z]{2}$",
    )


class TranscriptSegment(BaseModel):
    """Single segment of a transcript with timestamps."""

    start_sec: float
    end_sec: float
    text: str


class Transcript(BaseModel):
    """Result of audio transcription."""

    segments: list[TranscriptSegment]
    language: str | None = Field(
        default=None,
        description="ISO 639-1 language code detected or used for transcription.",
        pattern=r"^[a-z]{2}$",
    )


TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
"""Async callable: (audio_path, params) → Transcript.

First argument is the path to a WAV audio file on local disk.
"""


# ---------------------------------------------------------------------------
# Web scraping (trafilatura / future headless browser)
# ---------------------------------------------------------------------------


class ScrapeWebParams(BaseModel):
    """Parameters for web content extraction."""

    include_tables: bool = Field(
        default=True,
        description="Whether to include table content in extraction.",
    )
    include_comments: bool = Field(
        default=False,
        description="Whether to include user comments.",
    )


class ScrapedContent(BaseModel):
    """Result of web page scraping."""

    text: str = Field(description="Extracted main content as plain text.")
    raw_html: str = Field(description="Raw HTML for snapshot / re-processing.")


ScrapeWebFunc = Callable[
    [str, ScrapeWebParams],
    Awaitable[ScrapedContent],
]
"""Async callable: (url, params) → ScrapedContent.

First argument is the URL to fetch and extract content from.
"""
