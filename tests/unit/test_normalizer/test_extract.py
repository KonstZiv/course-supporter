"""Tests for DefaultTextExtractor (KD18 P1, extract-at-diff-time)."""

import io

from course_supporter.normalizer.extract import DefaultTextExtractor
from course_supporter.normalizer.models import EntryClass
from course_supporter.normalizer.ports import TextExtractor


def _docx_bytes(*paragraphs: str) -> bytes:
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _pdf_bytes(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


class TestText:
    def test_utf8_decode(self) -> None:
        assert DefaultTextExtractor().extract(EntryClass.TEXT, b"hello\nworld") == (
            "hello\nworld"
        )

    def test_undecodable_bytes_replaced_not_raised(self) -> None:
        out = DefaultTextExtractor().extract(EntryClass.TEXT, b"caf\xe9")
        assert out is not None
        assert out.startswith("caf")


class TestDocument:
    def test_docx_paragraphs(self) -> None:
        raw = _docx_bytes("Alpha line", "Beta line")
        out = DefaultTextExtractor().extract(EntryClass.DOCUMENT, raw)
        assert out is not None
        assert "Alpha line" in out
        assert "Beta line" in out

    def test_pdf_text(self) -> None:
        raw = _pdf_bytes("Gamma text")
        out = DefaultTextExtractor().extract(EntryClass.DOCUMENT, raw)
        assert out is not None
        assert "Gamma text" in out

    def test_document_routed_by_magic(self) -> None:
        # A docx and a pdf both classify as DOCUMENT; routing is by magic.
        ext = DefaultTextExtractor()
        assert "Delta" in (ext.extract(EntryClass.DOCUMENT, _pdf_bytes("Delta")) or "")
        assert "Delta" in (ext.extract(EntryClass.DOCUMENT, _docx_bytes("Delta")) or "")

    def test_same_bytes_same_text_deterministic(self) -> None:
        # One extractor, both sides: identical bytes yield identical text.
        raw = _docx_bytes("stable content")
        ext = DefaultTextExtractor()
        assert ext.extract(EntryClass.DOCUMENT, raw) == ext.extract(
            EntryClass.DOCUMENT, raw
        )


class TestBinaryAndPort:
    def test_binary_returns_none(self) -> None:
        assert (
            DefaultTextExtractor().extract(EntryClass.BINARY, bytes(range(64))) is None
        )

    def test_conforms_to_port(self) -> None:
        extractor: TextExtractor = DefaultTextExtractor()
        assert extractor.extract(EntryClass.TEXT, b"x") == "x"
