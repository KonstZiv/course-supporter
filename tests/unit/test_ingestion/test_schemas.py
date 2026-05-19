"""Tests for ingestion pipeline schemas and interfaces."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from course_supporter.ingestion.base import (
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.schemas import (
    AudioPass2aResult,
    AudioSegmentDraft,
    AudioSubsegmentDraft,
    PresentationPass2aResult,
    PresentationSegment,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)


class TestSourceType:
    def test_source_type_values(self) -> None:
        """All expected source types exist with correct string values."""
        assert SourceType.VIDEO == "video"
        assert SourceType.PRESENTATION == "presentation"
        assert SourceType.TEXT == "text"
        assert SourceType.WEB == "web"

    def test_source_type_matches_orm_enum(self) -> None:
        """SourceType values match ORM source_type_enum."""
        assert len(SourceType) == 5


class TestChunkType:
    def test_chunk_type_values(self) -> None:
        """All expected chunk types exist with correct string values."""
        assert ChunkType.TRANSCRIPT == "transcript"
        assert ChunkType.SLIDE_TEXT == "slide_text"
        assert ChunkType.PARAGRAPH == "paragraph"
        assert ChunkType.HEADING == "heading"
        assert ChunkType.WEB_CONTENT == "web_content"
        assert ChunkType.VISUAL_SCENE == "visual_scene"

    def test_chunk_type_orphans_removed(self) -> None:
        # Phase 2.3 KD-2.3-J orphan cleanup: ``METADATA`` (sub-area #1)
        # and ``SLIDE_DESCRIPTION`` (sub-area #4, bundled with the
        # PresentationProcessor rewrite that previously emitted it) are
        # both dropped. Gate 8 full verification.
        assert "METADATA" not in ChunkType.__members__
        assert "SLIDE_DESCRIPTION" not in ChunkType.__members__
        values = {c.value for c in ChunkType}
        assert "metadata" not in values
        assert "slide_description" not in values

    def test_chunk_type_keeps_visual_scene_and_slide_text(self) -> None:
        # Positive gate: KD-2.3-J explicitly preserves these enum
        # values. ``VISUAL_SCENE`` has an active producer in
        # ``ingestion/video.py``; ``SLIDE_TEXT`` becomes the canonical
        # per-slide chunk type after the sub-area #4 rewrite (v0.3 N3
        # empty-text filter).
        assert ChunkType.VISUAL_SCENE in ChunkType
        assert ChunkType.SLIDE_TEXT in ChunkType


class TestContentChunk:
    def test_content_chunk_default_metadata(self) -> None:
        """ContentChunk metadata defaults to empty dict."""
        chunk = ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="hello")
        assert chunk.metadata == {}
        assert chunk.index == 0

    def test_content_chunk_with_timecodes(self) -> None:
        """Transcript chunk carries start/end timecodes as top-level fields."""
        chunk = ContentChunk(
            chunk_type=ChunkType.TRANSCRIPT,
            text="Hello world",
            index=0,
            start_sec=0.0,
            end_sec=30.0,
        )
        assert chunk.start_sec == 0.0
        assert chunk.end_sec == 30.0


class TestSourceDocument:
    def test_source_document_defaults(self) -> None:
        """SourceDocument has empty chunks and auto processed_at."""
        doc = SourceDocument(source_type=SourceType.TEXT, source_url="file:///test.md")
        assert doc.chunks == []
        assert doc.title == ""
        assert isinstance(doc.processed_at, datetime)
        assert doc.metadata == {}

    def test_source_document_with_chunks(self) -> None:
        """SourceDocument holds multiple content chunks."""
        chunks = [
            ContentChunk(chunk_type=ChunkType.HEADING, text="Title", index=0),
            ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="Body", index=1),
        ]
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///test.md",
            title="My Doc",
            chunks=chunks,
        )
        assert len(doc.chunks) == 2
        assert doc.chunks[0].chunk_type == ChunkType.HEADING


class TestMaterialProcessor:
    def test_source_processor_is_abstract(self) -> None:
        """MaterialProcessor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            MaterialProcessor()  # type: ignore[abstract]

    def test_processing_error_hierarchy(self) -> None:
        """UnsupportedFormatError is a subclass of ProcessingError."""
        assert issubclass(UnsupportedFormatError, ProcessingError)
        assert issubclass(ProcessingError, Exception)


class TestAudioPass2aResultGates:
    """KD-2.2-H schema acceptance gates for Pass 2a output structure."""

    @staticmethod
    def _segment(
        start: int,
        end: int,
        *,
        noisy: bool = False,
        subsegments: list[AudioSubsegmentDraft] | None = None,
    ) -> AudioSegmentDraft:
        """Helper: build a minimal AudioSegmentDraft with required fields."""
        return AudioSegmentDraft(
            start_word_idx=start,
            end_word_idx=end,
            description="desc",
            noisy=noisy,
            subsegments=subsegments or [],
        )

    @staticmethod
    def _subsegment(
        start: int, end: int, *, noisy: bool = False
    ) -> AudioSubsegmentDraft:
        return AudioSubsegmentDraft(
            start_word_idx=start,
            end_word_idx=end,
            description="sub-desc",
            noisy=noisy,
        )

    def test_trailing_words_coverage(self) -> None:
        """_full_cover gate raises when last.end_word_idx < total_word_count.

        Closes the trailing-word undercoverage failure mode (KD-2.2-H prompt
        rule: 'last segment must cover trailing words'). Context-driven
        check via ValidationInfo.context["total_word_count"].
        """
        payload = {
            "description": "doc desc",
            "segments": [
                self._segment(0, 50).model_dump(),
                self._segment(50, 90).model_dump(),
            ],
        }
        import json as _json

        with pytest.raises(ValidationError, match="do not cover the word stream"):
            AudioPass2aResult.model_validate_json(
                _json.dumps(payload), context={"total_word_count": 100}
            )

    def test_subsegments_cover_parent(self) -> None:
        """_subsegments_cover_parent gate raises on parent-cover violation.

        Closes the DeepSeek mechanical failure mode (DD-2.2-X, 2026-05-15):
        parent.end_word_idx inherited from last subsegment leaves trailing
        words within parent uncovered.
        """
        bad_subs = [
            self._subsegment(0, 30),
            self._subsegment(30, 60),
        ]
        with pytest.raises(ValidationError, match="last subsegment must end"):
            AudioPass2aResult(
                description="doc",
                segments=[
                    self._segment(0, 100, subsegments=bad_subs),
                ],
            )

    def test_max_segments_enforced(self) -> None:
        """Pydantic max_length=10 enforces KD-2.2-H prompt segment cap."""
        too_many = [self._segment(i * 10, (i + 1) * 10) for i in range(11)]
        with pytest.raises(ValidationError, match="at most 10"):
            AudioPass2aResult(description="doc", segments=too_many)

    def test_max_subsegments_enforced(self) -> None:
        """Pydantic max_length=5 enforces KD-2.2-H prompt subsegment cap."""
        too_many_subs = [self._subsegment(i * 10, (i + 1) * 10) for i in range(6)]
        with pytest.raises(ValidationError, match="at most 5"):
            AudioSegmentDraft(
                start_word_idx=0,
                end_word_idx=60,
                description="desc",
                noisy=False,
                subsegments=too_many_subs,
            )

    def test_no_third_level_nesting(self) -> None:
        """Type system enforces 2-level max depth (KD-2.2-H).

        AudioSubsegmentDraft has no ``subsegments`` field — third-level
        nesting cannot be expressed in the Python type system, so even
        if an LLM emits a ``subsegments`` key on a leaf, Pydantic strips
        it (extra="ignore" default) and the resulting leaf has no way
        to surface deeper structure. Verify both legs of the
        enforcement: field absence + payload-key drop.
        """
        assert "subsegments" not in AudioSubsegmentDraft.model_fields

        payload = {
            "start_word_idx": 0,
            "end_word_idx": 10,
            "description": "leaf",
            "noisy": False,
            "subsegments": [{"start_word_idx": 0, "end_word_idx": 5}],
        }
        leaf = AudioSubsegmentDraft.model_validate(payload)
        assert not hasattr(leaf, "subsegments")
        # Round-trip also drops the field — third-level nesting
        # cannot propagate through Pydantic serialization.
        assert "subsegments" not in leaf.model_dump()

    def test_doc_level_fields_required(self) -> None:
        """Title/description Field validation per PHASE.md v0.3 contract.

        ``description`` required (must be present + non-None) with
        max_length=512 cap. ``title`` optional with max_length=128 cap and
        ``None`` default for the «talk without explicit theme» edge case.
        """
        with pytest.raises(ValidationError, match="description"):
            AudioPass2aResult.model_validate({"segments": []})

        long_title = "x" * 129
        with pytest.raises(ValidationError, match="at most 128"):
            AudioPass2aResult(
                title=long_title,
                description="doc",
                segments=[],
            )

        long_desc = "x" * 513
        with pytest.raises(ValidationError, match="at most 512"):
            AudioPass2aResult(
                description=long_desc,
                segments=[],
            )

        ok = AudioPass2aResult(title=None, description="ok doc", segments=[])
        assert ok.title is None
        assert ok.description == "ok doc"


# ── Phase 2.3 KD-2.3-Q — DocumentSegmentDraft presentation slide fields ──


class TestDocumentSegmentDraftSlideFields:
    """``start_slide`` / ``end_slide`` optionals for the presentation pipeline.

    Mirrors the audio Phase 2.2 ``start_time_sec`` / ``end_time_sec``
    pattern: optional with ``None`` defaults so existing audio/text/
    web/video callers stay backward-compatible without code changes.
    Populated by Pass 2b for the presentation source type via the
    slide-boundary bridge (KD-2.3-Q).
    """

    @staticmethod
    def _base_payload() -> dict[str, object]:
        return {
            "order": 0,
            "start_pos": 0,
            "end_pos": 100,
            "description": "Segment description.",
        }

    def test_start_slide_end_slide_default_to_none(self) -> None:
        from course_supporter.ingestion.schemas import DocumentSegmentDraft

        draft = DocumentSegmentDraft(**self._base_payload())  # type: ignore[arg-type]
        assert draft.start_slide is None
        assert draft.end_slide is None

    def test_start_slide_end_slide_accept_slide_numbers(self) -> None:
        from course_supporter.ingestion.schemas import DocumentSegmentDraft

        payload = self._base_payload() | {"start_slide": 3, "end_slide": 7}
        draft = DocumentSegmentDraft(**payload)  # type: ignore[arg-type]
        assert draft.start_slide == 3
        assert draft.end_slide == 7

    def test_existing_callers_unchanged_without_slide_fields(self) -> None:
        # Regression guard: audio/text/web/video callers never pass
        # the slide fields. Constructing a draft without them must
        # succeed (no required-field ValidationError) and leave both
        # fields at their ``None`` defaults so the audio Phase 2.2
        # ``start_time_sec`` / ``end_time_sec`` semantics carry over.
        from course_supporter.ingestion.schemas import DocumentSegmentDraft

        draft = DocumentSegmentDraft(
            order=0,
            start_pos=0,
            end_pos=50,
            description="Audio segment.",
            start_time_sec=0.0,
            end_time_sec=12.5,
        )
        assert draft.start_slide is None
        assert draft.end_slide is None
        assert draft.start_time_sec == 0.0
        assert draft.end_time_sec == 12.5


# ── Phase 2.3 KD-2.3-H — PresentationPass2aResult validator gates ───


class TestPresentationPass2aResultGates:
    """Structural invariants the spike v2 calibrated against."""

    @staticmethod
    def _segments(*ranges: tuple[int, int]) -> list[dict[str, object]]:
        return [
            {
                "start_slide": s,
                "end_slide": e,
                "title": f"Seg {s}-{e}",
                "description": f"Covers {s}-{e}.",
            }
            for s, e in ranges
        ]

    def test_valid_contiguous_segments(self) -> None:
        result = PresentationPass2aResult.model_validate(
            {
                "title": "Deck",
                "description": "A deck.",
                "segments": self._segments((1, 2), (3, 5)),
            }
        )
        assert len(result.segments) == 2
        assert isinstance(result.segments[0], PresentationSegment)

    def test_first_segment_must_start_at_slide_1(self) -> None:
        with pytest.raises(ValidationError, match="must start at slide 1"):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": self._segments((2, 3), (4, 5))}
            )

    def test_non_contiguous_segments_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be contiguous"):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": self._segments((1, 2), (4, 5))}
            )

    def test_miller_rule_lower_bound(self) -> None:
        with pytest.raises(ValidationError, match="Miller's rule"):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": self._segments((1, 5))}
            )

    def test_miller_rule_upper_bound(self) -> None:
        eight = self._segments(*[(i, i) for i in range(1, 9)])
        with pytest.raises(ValidationError, match="Miller's rule"):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": eight}
            )

    def test_end_slide_before_start_slide_rejected(self) -> None:
        with pytest.raises(ValidationError, match="end_slide >="):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": self._segments((1, 2), (3, 2))}
            )

    def test_last_segment_must_cover_n_slides_via_context(self) -> None:
        with pytest.raises(ValidationError, match="do not cover all slides"):
            PresentationPass2aResult.model_validate(
                {"description": "d", "segments": self._segments((1, 2), (3, 4))},
                context={"n_slides": 6},
            )

    def test_last_segment_cover_passes_with_matching_context(self) -> None:
        result = PresentationPass2aResult.model_validate(
            {"description": "d", "segments": self._segments((1, 2), (3, 4))},
            context={"n_slides": 4},
        )
        assert result.segments[-1].end_slide == 4

    def test_cover_check_skipped_without_context(self) -> None:
        # No context → coverage gate skipped; structural gate still runs.
        result = PresentationPass2aResult.model_validate(
            {"description": "d", "segments": self._segments((1, 2), (3, 9))}
        )
        assert result.segments[-1].end_slide == 9
