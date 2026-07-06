"""Extract-at-diff-time text extractor (KD18 P1, TextExtractor port).

``DefaultTextExtractor`` pulls review-facing text from a snapshot entry
on demand -- not at store time. docx/pdf are kept as raw bytes in the
snapshot (their hash detects change); text is extracted only when a
consumer (P4) needs it, and by ONE extractor for both base and
submission, so version drift in the docx/pdf tooling cannot desync the
two sides.

Reuses the established extraction patterns bytes-natively: PyMuPDF
``get_text`` (cf. ``ingestion/presentation.py``) and python-docx
paragraph walking (cf. ``ingestion/text.py``). Zero new dependencies.
"""

from __future__ import annotations

import io

from course_supporter.normalizer.models import EntryClass


class DefaultTextExtractor:
    """Type-driven text extractor (``TextExtractor`` port implementation).

    TEXT -> UTF-8 decode (undecodable bytes replaced, never raises);
    DOCUMENT -> pdf or docx text, routed by magic (``%PDF`` vs the docx
    zip container); BINARY -> ``None`` (outside the review scope).
    """

    def extract(self, cls: EntryClass, raw: bytes) -> str | None:
        if cls is EntryClass.TEXT:
            return raw.decode("utf-8", errors="replace")
        if cls is EntryClass.DOCUMENT:
            if raw.startswith(b"%PDF"):
                return _extract_pdf(raw)
            return _extract_docx(raw)
        return None


def _extract_pdf(raw: bytes) -> str:
    """Per-page text via PyMuPDF (bytes-native; no rendering)."""
    import fitz

    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def _extract_docx(raw: bytes) -> str:
    """Paragraph text via python-docx (bytes-native, no filesystem)."""
    from docx import Document

    document = Document(io.BytesIO(raw))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)
