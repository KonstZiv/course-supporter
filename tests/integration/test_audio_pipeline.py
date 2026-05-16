"""End-to-end AudioProcessor pipeline integration test (PHASE.md §3 acceptance).

Drives the full three-stage audio pipeline (process_raw → process_macro →
process_detail) on the Scribe v1 Ukrainian 124-min fixture using AsyncMock
STT + StageRouter harnesses. Verifies KD-2.2-D/E/F/A/I/K acceptance
together: word-cache round-trip across stages, cumsum bridge offset
correctness, Pass 2a metadata mapping, Pass 2c selective routing, and
combined factory dispatch wiring.

The fixture (committed in sub-area #8-9 C1, d81926f) contains the raw
Scribe v1 response shape with 27407 words across 124 minutes. The test
slices the first 100 words for speed; full-corpus exercise stays in the
RUN_SMOKE-gated smoke test (see test_audio_pipeline_smoke.py).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.schemas import (
    AudioPass2aResult,
    AudioSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.models.source import SourceType
from course_supporter.service_logging import set_job_from_arq
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.stt.schemas import STTResult, STTSegment, STTWord

_FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "audio"
    / "scribe_v1_ukrainian_124min.json"
)
_WORDS_SLICE = 100  # first 100 words for fast unit-test-speed integration


def _load_words_slice() -> list[STTWord]:
    """Load first 100 words from the committed Scribe v1 fixture."""
    payload = json.loads(_FIXTURE_PATH.read_text())
    raw_words = payload["words"][:_WORDS_SLICE]
    return [
        STTWord(
            text=w["text"],
            start_sec=w["start"],
            end_sec=w["end"],
            logprob=w.get("logprob"),
        )
        for w in raw_words
    ]


def _mock_stt_router(words: list[STTWord]) -> AsyncMock:
    """AsyncMock STTRouter returning a single-segment STTResult."""
    segment = STTSegment(
        start_sec=words[0].start_sec,
        end_sec=words[-1].end_sec,
        text=" ".join(w.text for w in words),
    )
    result = STTResult(
        text=segment.text,
        segments=[segment],
        words=words,
        provider="mock-scribe",
        model_id="mock-scribe-v2",
        audio_duration_sec=words[-1].end_sec,
    )
    router = AsyncMock()
    router.transcribe.return_value = result
    return router


def _mock_redis() -> AsyncMock:
    """In-memory AsyncMock standing in for ArqRedis SET/GET."""
    store: dict[str, str] = {}

    async def _set(key: str, value: str, ex: int | None = None) -> None:
        store[key] = value

    async def _get(key: str) -> str | None:
        return store.get(key)

    redis = AsyncMock()
    redis.set.side_effect = _set
    redis.get.side_effect = _get
    return redis


def _mock_authored_document() -> Mock:
    """AuthoredDocument stand-in with spec_set for attribute-access safety."""
    doc = Mock(spec_set=AuthoredDocument)
    doc.source_type = SourceType.AUDIO
    doc.source_url = "/tmp/fixture-audio.mp3"
    doc.filename = "scribe_v1_ukrainian_124min.mp3"
    return doc


def _build_pass2a_result(word_count: int) -> AudioPass2aResult:
    """Construct a realistic 3-segment AudioPass2aResult covering the slice.

    Segment 0 (words 0-40) — clean intro.
    Segment 1 (words 40-70) — noisy=True (filler, hesitation).
    Segment 2 (words 70-word_count) — clean conclusion.
    """
    seg0_end = min(40, word_count)
    seg1_end = min(70, word_count)
    return AudioPass2aResult(
        title="Test fixture topic",
        description="A 100-word slice of a 124-min Ukrainian lecture.",
        segments=[
            AudioSegmentDraft(
                start_word_idx=0,
                end_word_idx=seg0_end,
                title="Intro",
                description="Opening of the lecture.",
                main_concepts=["topic-a"],
                secondary_concepts=["topic-b"],
                noisy=False,
            ),
            AudioSegmentDraft(
                start_word_idx=seg0_end,
                end_word_idx=seg1_end,
                title="Body (noisy)",
                description="Middle of the lecture with filler words.",
                main_concepts=["topic-c"],
                secondary_concepts=["topic-a"],
                noisy=True,
            ),
            AudioSegmentDraft(
                start_word_idx=seg1_end,
                end_word_idx=word_count,
                title="Conclusion",
                description="Closing remarks.",
                main_concepts=["topic-d"],
                secondary_concepts=[],
                noisy=False,
            ),
        ],
    )


def _mock_stage_router(pass2a: AudioPass2aResult) -> AsyncMock:
    """StageRouter dispatching by stage name to Pass 2a or Pass 2c shapes."""
    router = AsyncMock()

    async def _execute(
        stage: str,
        *,
        response_validator: Callable[[str], None],
        **template_kwargs: object,
    ) -> MagicMock:
        if stage == "audio_pass_2a_mapping":
            response_validator(pass2a.model_dump_json())
        elif stage == "audio_pass_2c_denoise":
            response_validator("[cleaned] denoised content")
        else:  # pragma: no cover — defensive
            msg = f"Unexpected stage: {stage}"
            raise AssertionError(msg)
        out = MagicMock()
        out.provider_used = "mock-llm"
        out.model_used = "mock-model"
        out.attempt_count = 1
        return out

    router.execute_for_stage.side_effect = _execute
    return router


# ──────────────────────────────────────────────────────────────────────
# End-to-end pipeline tests.
# ──────────────────────────────────────────────────────────────────────


class TestAudioPipelineEndToEnd:
    """KD-2.2-D/E/F/A/I/K — full three-stage pipeline acceptance."""

    async def test_full_pipeline_happy_path(self) -> None:
        """process_raw → process_macro → process_detail on fixture words."""
        words = _load_words_slice()
        assert len(words) == _WORDS_SLICE

        stt_router = _mock_stt_router(words)
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=stt_router, redis=redis)
        set_job_from_arq(uuid.uuid4())

        # Stage 1 — STT + cache + chunks + critical invariant assert.
        doc = await proc.process_raw(_mock_authored_document())
        assert doc.source_type == SourceType.AUDIO
        assert len(doc.chunks) == 1
        reference_text = doc.assemble_text()
        assert reference_text == " ".join(w.text for w in words)

        # Stage 2 — Pass 2a mapping + three pass-through + cumsum bridge.
        pass2a = _build_pass2a_result(len(words))
        stage_router = _mock_stage_router(pass2a)
        summary = await proc.process_macro(doc, stage_router)
        assert isinstance(summary, DocumentSummaryDraft)
        assert summary.title == "Test fixture topic"
        assert summary.description.startswith("A 100-word slice")
        assert len(summary.segments) == 3
        # Concept aggregation conflict rule — topic-a in seg 0 main_concepts
        # promotes it out of seg 1 secondary_concepts in final aggregation.
        assert "topic-a" in summary.main_concepts
        assert "topic-a" not in summary.secondary_concepts
        # cumsum bridge — first segment starts at 0; last ends at reference.
        assert summary.segments[0].start_pos == 0
        assert summary.segments[-1].end_pos == len(reference_text)
        # noisy field propagated from AudioSegmentDraft → DocumentSegmentDraft.
        assert summary.segments[1].noisy is True
        assert summary.segments[0].noisy is False
        assert summary.segments[2].noisy is False

        # Stage 3 — Pass 2b slice (every segment) + Pass 2c on noisy=True.
        detail = await proc.process_detail(doc, summary, router=stage_router)
        assert len(detail) == 3
        # Pass 2c invoked exactly once для seg 1 (noisy=True); seg 0 + seg 2
        # retain raw slice content.
        assert detail[1].content == "[cleaned] denoised content"
        assert (
            detail[0].content == reference_text[detail[0].start_pos : detail[0].end_pos]
        )
        assert (
            detail[2].content == reference_text[detail[2].start_pos : detail[2].end_pos]
        )
        # Total stage_router invocations: 1 Pass 2a + 1 Pass 2c (seg 1 only)
        assert stage_router.execute_for_stage.await_count == 2

    async def test_segment_contiguity_preserved_end_to_end(self) -> None:
        """KD-2.2-F invariant — segment offsets cover reference text fully."""
        words = _load_words_slice()
        stt_router = _mock_stt_router(words)
        proc = AudioProcessor(stt_router=stt_router, redis=_mock_redis())
        set_job_from_arq(uuid.uuid4())

        doc = await proc.process_raw(_mock_authored_document())
        reference_text = doc.assemble_text()

        pass2a = _build_pass2a_result(len(words))
        stage_router = _mock_stage_router(pass2a)
        summary = await proc.process_macro(doc, stage_router)

        # Segments are contiguous (start_pos[k] == end_pos[k-1]) and cover
        # the full reference_text — DocumentSummaryDraft validator already
        # enforces this; explicit re-check in integration scope guards
        # against silent cumsum-bridge regression.
        prev_end = 0
        for seg in summary.segments:
            assert seg.start_pos == prev_end
            assert seg.end_pos > seg.start_pos
            prev_end = seg.end_pos
        assert prev_end == len(reference_text)
