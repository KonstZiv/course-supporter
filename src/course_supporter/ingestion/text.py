"""Text processor for MD, DOCX, HTML, and plain text files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from course_supporter.ingestion.base import (
    SourceProcessor,
    UnsupportedFormatError,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import SourceMaterial

logger = structlog.get_logger()

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".docx", ".html", ".htm", ".txt"}


class TextProcessor(SourceProcessor):
    """Process text documents (MD, DOCX, HTML, TXT).

    Extracts headings and paragraphs without LLM.
    The router parameter is accepted but not used.
    """

    async def process(
        self,
        source: SourceMaterial,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.TEXT:
            raise UnsupportedFormatError(
                f"TextProcessor expects 'text', got '{source.source_type}'"
            )

        path = Path(source.source_url)
        ext = path.suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported text format: {ext}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        logger.info(
            "text_processing_start",
            source_url=source.source_url,
            format=ext,
        )

        if ext in {".md", ".markdown"}:
            chunks = self._process_markdown(path)
        elif ext == ".docx":
            chunks = self._process_docx(path)
        elif ext in {".html", ".htm"}:
            chunks = self._process_html(path)
        else:  # .txt
            chunks = self._process_plain_text(path)

        logger.info(
            "text_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type=SourceType.TEXT,
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
        )

    def _process_markdown(self, path: Path) -> list[ContentChunk]:
        """Parse Markdown file into heading/paragraph chunks.

        Headings: lines starting with # (1-6 levels).
        Paragraphs: non-empty text between headings.
        """
        content = path.read_text(encoding="utf-8")
        return self._parse_markdown_text(content)

    @staticmethod
    def _parse_markdown_text(content: str) -> list[ContentChunk]:
        """Parse markdown text content into chunks."""
        chunks: list[ContentChunk] = []
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

        idx = 0
        last_end = 0

        for match in heading_pattern.finditer(content):
            # Text before this heading → paragraph
            before = content[last_end : match.start()].strip()
            if before:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=before,
                        index=idx,
                    )
                )
                idx += 1

            # Heading itself
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.HEADING,
                    text=heading_text,
                    index=idx,
                    metadata={"level": level},
                )
            )
            idx += 1
            last_end = match.end()

        # Remaining text after last heading
        remaining = content[last_end:].strip()
        if remaining:
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.PARAGRAPH,
                    text=remaining,
                    index=idx,
                )
            )

        return chunks

    @staticmethod
    def _process_docx(path: Path) -> list[ContentChunk]:
        """Parse DOCX file into heading/paragraph chunks.

        Detects heading styles (Heading 1..6) and maps to levels.
        """
        from docx import Document

        doc = Document(str(path))
        chunks: list[ContentChunk] = []
        idx = 0

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""

            if style_name.startswith("Heading"):
                # Extract level from "Heading 1", "Heading 2", etc.
                try:
                    level = int(style_name.split()[-1])
                except (ValueError, IndexError):
                    level = 1

                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.HEADING,
                        text=text,
                        index=idx,
                        metadata={"level": level},
                    )
                )
            else:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=text,
                        index=idx,
                    )
                )
            idx += 1

        return chunks

    @staticmethod
    def _process_html(path: Path) -> list[ContentChunk]:
        """Parse HTML file into heading/paragraph chunks.

        Uses BeautifulSoup to extract <h1>..<h6> and <p> elements.
        """
        from bs4 import BeautifulSoup

        html = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        chunks: list[ContentChunk] = []
        idx = 0

        for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
            text = element.get_text(strip=True)
            if not text:
                continue

            if element.name.startswith("h"):
                level = int(element.name[1])
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.HEADING,
                        text=text,
                        index=idx,
                        metadata={"level": level},
                    )
                )
            else:
                chunks.append(
                    ContentChunk(
                        chunk_type=ChunkType.PARAGRAPH,
                        text=text,
                        index=idx,
                    )
                )
            idx += 1

        return chunks

    @staticmethod
    def _process_plain_text(path: Path) -> list[ContentChunk]:
        """Read plain text file as a single paragraph chunk."""
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        return [
            ContentChunk(
                chunk_type=ChunkType.PARAGRAPH,
                text=content,
                index=0,
            )
        ]
