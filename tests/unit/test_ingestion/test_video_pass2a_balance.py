"""Tests for the Pass 2a balance gate (TASK-2.4.19).

CI-level: pure helper (boundary cases) + ``step_5_pass2a_mapping``
integration scenarios exercising the validator's anti-dominant guard and
the degraded-catch fork in the caller. The fake router walks a flat
content sequence as one attempt per slot so retry + cascade reduce to a
linear list — sufficient to verify validator path (raise vs accept) and
the caller's ``LadderExhaustedError`` handling.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.ingestion.schemas import AudioSegmentDraft, DocumentSummaryDraft
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.ingestion.video_pipeline.schemas import SttResult, SttWord
from course_supporter.ingestion.video_pipeline.steps import (
    _BALANCE_THRESHOLD,
    largest_segment_share,
)
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

_GET_JOB_ID = "course_supporter.ingestion.video_pipeline.steps.get_current_job_id"
_JOB_ID = uuid.UUID("00000000-0000-0000-0000-000000000019")


def _seg(start: int, end: int, title: str = "t") -> AudioSegmentDraft:
    return AudioSegmentDraft(
        start_word_idx=start,
        end_word_idx=end,
        title=title,
        description="d",
        main_concepts=[],
        secondary_concepts=[],
        noisy=False,
        subsegments=[],
    )


class TestLargestSegmentShare:
    """Pure helper — Pass 2a balance metric."""

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty segments"):
            largest_segment_share([])

    def test_single_segment(self) -> None:
        # max == sum → 1.0
        assert largest_segment_share([_seg(0, 10)]) == 1.0

    def test_balanced_well_below_threshold(self) -> None:
        # spans 40 / 35 / 25 → max 40, sum 100 → 0.40
        segs = [_seg(0, 40), _seg(40, 75), _seg(75, 100)]
        assert largest_segment_share(segs) == pytest.approx(0.40)

    def test_exact_threshold_boundary_does_not_trigger(self) -> None:
        # spans 45 / 45 / 10 → max 45, sum 100 → 0.45 exactly.
        # Gate uses strict ``>`` — 0.45 must not trigger.
        segs = [_seg(0, 45), _seg(45, 90), _seg(90, 100)]
        result = largest_segment_share(segs)
        assert result == pytest.approx(0.45)
        assert result <= _BALANCE_THRESHOLD

    def test_just_above_threshold_triggers(self) -> None:
        # spans 46 / 45 / 9 → max 46, sum 100 → 0.46
        segs = [_seg(0, 46), _seg(46, 91), _seg(91, 100)]
        result = largest_segment_share(segs)
        assert result == pytest.approx(0.46)
        assert result > _BALANCE_THRESHOLD

    def test_just_below_threshold_does_not_trigger(self) -> None:
        # spans 35 / 35 / 30 → max 35, sum 100 → 0.35
        segs = [_seg(0, 35), _seg(35, 70), _seg(70, 100)]
        result = largest_segment_share(segs)
        assert result == pytest.approx(0.35)
        assert result < _BALANCE_THRESHOLD


# ── Step 5 integration ───────────────────────────────────────────────────


def _words(*specs: tuple[str, int, int]) -> list[SttWord]:
    return [SttWord(text=t, start_ms=s, end_ms=e) for t, s, e in specs]


def _video_doc(words: list[SttWord]) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.VIDEO,
        source_url="s3://b/v.mp4",
        title="v",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.TRANSCRIPT,
                text=" ".join(w.text for w in words),
                index=0,
            )
        ],
    )


def _redis_with(stt: SttResult) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=stt.model_dump_json())
    return redis


def _result_json(spans: list[tuple[int, int]], title: str = "Doc") -> str:
    """Build a valid ``AudioPass2aResult`` JSON with the given word ranges."""
    return json.dumps(
        {
            "title": title,
            "description": "Doc-level description.",
            "segments": [
                {
                    "start_word_idx": s,
                    "end_word_idx": e,
                    "title": f"Seg-{i}",
                    "description": f"Description of segment {i}.",
                    "main_concepts": [],
                    "secondary_concepts": [],
                    "noisy": False,
                    "subsegments": [],
                }
                for i, (s, e) in enumerate(spans)
            ],
        }
    )


class _LadderFakeRouter:
    """Walks a content sequence and mirrors ``StageRouter`` validator semantics.

    Each item is one attempt slot. If the validator raises
    ``StructuralRetryError`` — that slot is exhausted and we advance. If
    it returns cleanly — we return ``StageResult``. After all items are
    exhausted — we raise ``LadderExhaustedError``. Models retry +
    cascade as a flat list; sufficient for verifying the validator
    path (raise vs accept) and the caller's exhaustion handling.
    """

    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[str] = []

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        contents: Any = None,
        **render_context: Any,
    ) -> StageResult:
        last_exc: StructuralRetryError | None = None
        for content in self.contents:
            self.calls.append(content)
            try:
                if response_validator is not None:
                    response_validator(content)
            except StructuralRetryError as exc:
                last_exc = exc
                continue
            return StageResult(
                content=content,
                provider_used="p",
                model_used="m",
                attempt_count=1,
            )
        raise LadderExhaustedError(
            stage_name,
            [("p", "m", str(last_exc) if last_exc else "no reason")],
        )


class TestStep5BalanceGate:
    """Validator + caller integration via a sequence-walking fake router."""

    async def test_balanced_parse_passes_through(self) -> None:
        """share ≤ 0.45 → normal flow, single validator call."""
        words = _words(*[(f"w{i}", i * 100, i * 100 + 50) for i in range(10)])
        stt = SttResult(language="en", duration_ms=1000, words=words, pauses=[])
        doc = _video_doc(words)
        # spans 4 / 3 / 3 → max 4/10 = 0.40 (≤ 0.45)
        balanced = _result_json([(0, 4), (4, 7), (7, 10)])
        router = _LadderFakeRouter([balanced])

        with patch(_GET_JOB_ID, return_value=_JOB_ID):
            summary = await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=router
            )

        assert isinstance(summary, DocumentSummaryDraft)
        assert len(summary.segments) == 3
        # Only one attempt — no retry on a balanced parse.
        assert len(router.calls) == 1

    async def test_balance_retry_followed_by_balanced_succeeds(self) -> None:
        """Imbalanced rung 1 → balanced rung 2 succeeds; no degraded path."""
        words = _words(*[(f"w{i}", i * 100, i * 100 + 50) for i in range(10)])
        stt = SttResult(language="en", duration_ms=1000, words=words, pauses=[])
        doc = _video_doc(words)
        imbalanced = _result_json([(0, 8), (8, 10)])  # max 8/10 = 0.80
        balanced = _result_json([(0, 4), (4, 7), (7, 10)])  # max 4/10 = 0.40
        router = _LadderFakeRouter([imbalanced, balanced])

        with patch(_GET_JOB_ID, return_value=_JOB_ID):
            summary = await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=router
            )

        assert isinstance(summary, DocumentSummaryDraft)
        assert len(summary.segments) == 3
        # Two attempts: imbalanced rejected (retry), balanced accepted.
        assert len(router.calls) == 2

    async def test_degraded_acceptance_when_all_rungs_imbalanced(self) -> None:
        """Every rung produces valid-but-imbalanced parse → degraded fallback."""
        words = _words(*[(f"w{i}", i * 100, i * 100 + 50) for i in range(10)])
        stt = SttResult(language="en", duration_ms=1000, words=words, pauses=[])
        doc = _video_doc(words)
        # spans 8 / 2 → max 8/10 = 0.80 (> 0.45), sustained over all attempts.
        imbalanced = _result_json([(0, 8), (8, 10)])
        # Two rungs x one structural retry each -> 4 attempts, all imbalanced.
        router = _LadderFakeRouter([imbalanced] * 4)

        with patch(_GET_JOB_ID, return_value=_JOB_ID):
            summary = await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=router
            )

        # Degraded path: last valid parse promoted to result.
        assert isinstance(summary, DocumentSummaryDraft)
        assert len(summary.segments) == 2
        # Every imbalanced attempt invoked the validator.
        assert len(router.calls) == 4

    async def test_schema_exhaustion_without_fallback_re_raises(self) -> None:
        """All rungs return schema-invalid JSON → no fallback → propagation."""
        words = _words(*[(f"w{i}", i * 100, i * 100 + 50) for i in range(10)])
        stt = SttResult(language="en", duration_ms=1000, words=words, pauses=[])
        doc = _video_doc(words)
        # Invalid JSON triggers ValidationError → StructuralRetryError on the
        # schema path (NOT the balance path); parsed["fallback"] never set.
        bogus = json.dumps({"not": "a valid result"})
        router = _LadderFakeRouter([bogus] * 4)

        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            pytest.raises(LadderExhaustedError),
        ):
            await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=router
            )

    async def test_balance_feedback_text_is_self_contained(self) -> None:
        """Balance feedback names dominant segment + word range + threshold."""
        words = _words(*[(f"w{i}", i * 100, i * 100 + 50) for i in range(10)])
        stt = SttResult(language="en", duration_ms=1000, words=words, pauses=[])
        doc = _video_doc(words)
        # spans 0..7 / 7..10 → max 7/10 = 0.70 — dominant idx 0, range 0-7.
        imbalanced = _result_json([(0, 7), (7, 10)], title="Doc")

        captured: list[str] = []

        class _CaptureRouter:
            async def execute_for_stage(
                self,
                stage_name: str,
                *,
                response_validator: Any = None,
                contents: Any = None,
                **render_context: Any,
            ) -> StageResult:
                assert response_validator is not None
                try:
                    response_validator(imbalanced)
                except StructuralRetryError as exc:
                    captured.append(exc.feedback)
                    raise LadderExhaustedError(
                        "video_pass_2a_mapping",
                        [("p", "m", exc.feedback)],
                    ) from None
                msg = "unreachable: balance gate should fire"
                raise AssertionError(msg)

        with patch(_GET_JOB_ID, return_value=_JOB_ID):
            # Expect degraded acceptance after the single-attempt exhaustion.
            await steps.step_5_pass2a_mapping(
                doc, redis=_redis_with(stt), stage_router=_CaptureRouter()
            )

        assert len(captured) == 1
        feedback = captured[0]
        # Dominant segment idx + title.
        assert "segment #0" in feedback
        assert '"Seg-0"' in feedback
        # Concrete word range of the dominant segment.
        assert "words 0-7" in feedback
        # Total word count for context.
        assert "of 10" in feedback
        # Threshold percentage as a hard constraint, not a generic retry plea.
        assert "at most 45%" in feedback
        # Explicit instruction to split that range, not generic "retry".
        assert "Split this range" in feedback
        # Must NOT collapse to the generic _structural_retry suffix.
        assert "Please retry with a valid response" not in feedback
