"""Unit tests for the Krok 5 Pass 2a word-idx mapping mechanism (Phase 2.4).

CI-level (no real LLM, no Redis server): the ``assemble_text`` /
``safety_text`` split (incl. the D4 byte-identity guarantee), the
deterministic visual-overlay word-anchor, the ``chars_per_word_cumsum``
bridge reuse, and ``step_5_pass2a_mapping`` against a fake StageRouter +
mocked Redis carrier. Text-pool ranking / segment-quality is the
operator-run RUN_SMOKE spike.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.ingestion.audio import chars_per_word_cumsum
from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.ingestion.video_pipeline.schemas import SttResult, SttWord
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

_PKG = "course_supporter.ingestion.video_pipeline"
_GET_JOB_ID = f"{_PKG}.steps.get_current_job_id"
_JOB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")

# ── builders ─────────────────────────────────────────────────────────────


def _words(*specs: tuple[str, int, int]) -> list[SttWord]:
    return [SttWord(text=t, start_ms=s, end_ms=e) for t, s, e in specs]


def _video_doc(words: list[SttWord], visuals: list[tuple[int, str]]) -> SourceDocument:
    """A video SourceDocument: one TRANSCRIPT chunk + VISUAL_SCENE chunks (B')."""
    chunks = [
        ContentChunk(
            chunk_type=ChunkType.TRANSCRIPT,
            text=" ".join(w.text for w in words),
            index=0,
        )
    ]
    for i, (ms, desc) in enumerate(visuals):
        chunks.append(
            ContentChunk(
                chunk_type=ChunkType.VISUAL_SCENE,
                text=desc,
                index=i + 1,
                start_sec=ms / 1000.0,
                metadata={"frame_position_ms": ms, "kind": "anchor", "scene_id": i},
            )
        )
    return SourceDocument(
        source_type=SourceType.VIDEO,
        source_url="s3://b/v.mp4",
        title="v",
        chunks=chunks,
    )


def _one_segment_json(render_context: dict[str, Any]) -> str:
    """A balanced AudioPass2aResult JSON covering the whole word stream.

    Splits the word stream evenly across the available words so the
    Pass 2a balance gate (TASK-2.4.19) passes — single-segment output
    is rejected by the validator. Concepts and ``noisy`` are concentrated
    on the first segment so existing aggregation assertions stay valid.
    """
    n = render_context["words_count"]
    boundaries = [round(i * n / 3) for i in range(4)]
    return json.dumps(
        {
            "title": "Theme",
            "description": "Doc-level description.",
            "segments": [
                {
                    "start_word_idx": boundaries[i],
                    "end_word_idx": boundaries[i + 1],
                    "title": f"Seg-{i}",
                    "description": f"Segment {i} description.",
                    "main_concepts": ["concept-a"] if i == 0 else [],
                    "secondary_concepts": ["concept-b"] if i == 0 else [],
                    "noisy": i == 0,
                    "subsegments": [],
                }
                for i in range(3)
            ],
        }
    )


class _FakeStageRouter:
    """StageRouter double: records calls, echoes a valid Pass 2a JSON."""

    def __init__(self, content_fn: Any = _one_segment_json) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content_fn = content_fn

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        contents: Any = None,
        **render_context: Any,
    ) -> StageResult:
        self.calls.append({"stage": stage_name, "render_context": render_context})
        content = self._content_fn(render_context)
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used="gemini",
            model_used="gemini-2.5-flash",
            attempt_count=1,
        )


def _redis_with(stt: SttResult) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=stt.model_dump_json())
    return redis


# ── assemble_text / safety_text split (D4 byte-identity) ──────────────────


class TestReferenceSurfaces:
    def test_assemble_text_excludes_visual_scene(self) -> None:
        doc = _video_doc(
            _words(("alpha", 0, 500), ("beta", 600, 1000)), [(700, "Slide")]
        )
        assert doc.assemble_text() == "alpha beta"
        assert "Slide" not in doc.assemble_text()

    def test_safety_text_is_byte_identical_to_pre_rework(self) -> None:
        # D4: video safety_text == "\n\n"-join of ALL chunks (the pre-2.4.5
        # assemble_text behaviour), so the Stage 2 surface is unchanged.
        doc = _video_doc(
            _words(("alpha", 0, 500), ("beta", 600, 1000)), [(700, "Slide")]
        )
        expected_pre_rework = "\n\n".join(c.text for c in doc.chunks if c.text)
        assert doc.safety_text() == expected_pre_rework
        assert "Slide" in doc.safety_text()  # visual still vetted by Stage 2

    def test_safety_text_defaults_to_assemble_text_for_non_video(self) -> None:
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="s3://b/a.mp3",
            title="a",
            chunks=[
                ContentChunk(chunk_type=ChunkType.TRANSCRIPT, text="one two", index=0)
            ],
        )
        assert doc.safety_text() == doc.assemble_text() == "one two"


# ── visual-overlay (deterministic word-anchor) ────────────────────────────


class TestVisualOverlay:
    def test_fmt_ts_minutes_can_exceed_59(self) -> None:
        assert steps._fmt_ts(5_000) == "00:05"
        assert steps._fmt_ts(83_000) == "01:23"
        assert steps._fmt_ts(9_300_000) == "155:00"  # > 150 min, no overflow

    def test_word_anchor_is_last_word_started_before_frame(self) -> None:
        words = _words(("a", 0, 100), ("b", 5_000, 5_100))
        doc = _video_doc(words, [(5_050, "diagram")])
        # frame at 5050ms → "b" (starts 5000 <= 5050) is the anchor word, idx 1.
        assert steps._build_visual_overlay(doc, words) == "[word ~1, 00:05] diagram"

    def test_anchor_reads_metadata_not_start_sec(self) -> None:
        words = _words(("a", 0, 100), ("b", 2_000, 2_100))
        doc = _video_doc(words, [(2_050, "slide")])
        # corrupt start_sec to prove metadata frame_position_ms is what's read.
        doc.chunks[1].start_sec = 99.0
        assert "[word ~1, 00:02]" in steps._build_visual_overlay(doc, words)

    def test_empty_when_no_visuals_or_no_words(self) -> None:
        words = _words(("a", 0, 100))
        assert steps._build_visual_overlay(_video_doc(words, []), words) == ""
        assert steps._build_visual_overlay(_video_doc(words, [(0, "x")]), []) == ""


# ── chars_per_word_cumsum reuse (D3 Protocol) ─────────────────────────────


class TestBridgeReuse:
    def test_cumsum_accepts_video_sttword(self) -> None:
        # video SttWord satisfies the SupportsText protocol structurally.
        words = _words(("ab", 0, 1), ("cde", 2, 3))
        assert chars_per_word_cumsum(words) == [0, 3, 6]  # == len("ab cde")


# ── step_5_pass2a_mapping orchestrator ────────────────────────────────────


class TestStep5:
    async def test_maps_word_idx_to_offsets_and_aggregates(self) -> None:
        words = _words(("alpha", 0, 500), ("beta", 600, 1_000), ("gamma", 1_200, 1_800))
        stt = SttResult(language="en", duration_ms=2_000, words=words, pauses=[])
        doc = _video_doc(words, [(800, "Slide: for-loop")])
        router = _FakeStageRouter()

        with patch(_GET_JOB_ID, return_value=_JOB_ID):
            summary = await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=router
            )

        assert isinstance(summary, DocumentSummaryDraft)
        # Balanced 3-segment fixture: first segment starts at 0, last ends at
        # end of the transcript — covers the word-idx → char-offset bridge.
        first = summary.segments[0]
        last = summary.segments[-1]
        assert first.start_pos == 0
        assert last.end_pos == len(doc.assemble_text()) == 16  # "alpha beta gamma"
        # timestamps adapted ms → sec (the single audio→video difference).
        assert first.start_time_sec == 0.0
        assert last.end_time_sec == 1.8
        # noisy concentrated on the first segment in the fixture.
        assert first.noisy is True
        # concept union+dedup (secondary minus main).
        assert summary.main_concepts == ["concept-a"]
        assert summary.secondary_concepts == ["concept-b"]
        # the visual-overlay reached the mapping LLM, anchored to a word.
        call = router.calls[0]
        assert call["stage"] == "video_pass_2a_mapping"
        assert call["render_context"]["words_count"] == 3
        assert "[word ~" in call["render_context"]["visuals"]

    async def test_cache_miss_raises_processing_error(self) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        doc = _video_doc(_words(("a", 0, 100)), [])

        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            pytest.raises(ProcessingError, match="cache miss"),
        ):
            await steps.step_5_pass2a_mapping(
                doc, redis=redis, stage_router=_FakeStageRouter()
            )

    async def test_missing_job_id_raises_processing_error(self) -> None:
        doc = _video_doc(_words(("a", 0, 100)), [])
        with (
            patch(_GET_JOB_ID, return_value=None),
            pytest.raises(ProcessingError, match="job_id"),
        ):
            await steps.step_5_pass2a_mapping(
                doc, redis=AsyncMock(), stage_router=_FakeStageRouter()
            )
