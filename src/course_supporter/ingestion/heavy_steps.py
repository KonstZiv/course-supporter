"""Heavy step protocols and param/result models.

Defines typed contracts for all heavy (serverless-ready) operations:
- Transcription (Whisper / Gemini)
- Slide image description (Vision LLM)
- PDF text extraction (fitz / OCR)
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
        pattern=r"^[a-z]{2}$",
    )


TranscribeFunc = Callable[[str, TranscribeParams], Awaitable[Transcript]]
"""Async callable: (audio_path, params) → Transcript.

First argument is the path to a WAV audio file on local disk.
"""


# ---------------------------------------------------------------------------
# Slide / image description (Vision LLM)
# ---------------------------------------------------------------------------


class DescribeSlidesParams(BaseModel):
    """Parameters for slide image description via Vision LLM."""

    dpi: int = Field(
        default=150,
        description="Resolution for rendering PDF pages to images.",
    )
    prompt: str = Field(
        default=(
            "Describe this slide. "
            "Focus on diagrams, charts, and key visual elements. "
            "Ignore decorative elements."
        ),
        description="Prompt sent to the Vision model for each slide.",
    )


class SlideDescription(BaseModel):
    """Vision LLM description of a single slide image."""

    slide_number: int
    description: str


DescribeSlidesFunc = Callable[
    [str, DescribeSlidesParams],
    Awaitable[list[SlideDescription]],
]
"""Async callable: (pdf_path, params) → list[SlideDescription].

First argument is the path to a PDF file on local disk.
Returns descriptions for every page that has visual content.
"""


# ---------------------------------------------------------------------------
# PDF text extraction (fitz / OCR / future Lambda)
# ---------------------------------------------------------------------------


class ParsePDFParams(BaseModel):
    """Parameters for PDF text extraction."""

    ocr_enabled: bool = Field(
        default=False,
        description="Whether to run OCR on image-only pages.",
    )


class PDFPageText(BaseModel):
    """Extracted text from a single PDF page."""

    page_number: int
    text: str


ParsePDFFunc = Callable[
    [str, ParsePDFParams],
    Awaitable[list[PDFPageText]],
]
"""Async callable: (pdf_path, params) → list[PDFPageText].

First argument is the path to a PDF file on local disk.
Returns extracted text for every page that has content.
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
