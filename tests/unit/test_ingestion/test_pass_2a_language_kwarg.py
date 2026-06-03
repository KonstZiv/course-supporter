"""Pass 2a ``language`` kwarg forwarding tests (task 2.4.14).

Each of the four MaterialProcessor implementations that drive a Pass 2a
stage call (text, web, audio, video) must forward
``language=display_name(doc.language)`` — or ``None`` when
``doc.language`` is unset — into ``StageRouter.execute_for_stage``. The
kwarg is the load-bearing render-context variable consumed by the
``{% if language %}`` gate in each prompt; missing it would land us
back in the legacy "detect from input" branch and silently un-do the
pin.

These tests assert the call-site contract only (no live LLM). The
prompt-side branch coverage lives in
``tests/unit/test_prompts/test_pass_2a_language_pin.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.ingestion.video_pipeline.schemas import SttResult, SttWord
from course_supporter.ingestion.web import WebProcessor
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.service_logging import set_job_from_arq
from course_supporter.stt.schemas import STTWord


def _captured_router(payload: str) -> AsyncMock:
    """StageRouter mock that records render_context per call."""
    router = AsyncMock()

    async def _execute(
        _stage_name: str,
        *,
        response_validator: Any | None = None,
        **render_context: Any,
    ) -> StageResult:
        if response_validator is not None:
            response_validator(payload)
        return StageResult(
            content=payload,
            provider_used="mock",
            model_used="mock-model",
            attempt_count=1,
        )

    router.execute_for_stage.side_effect = _execute
    return router


_EMPTY_PASS_2A_PAYLOAD = json.dumps({"title": "T", "description": "D.", "segments": []})


class TestTextProcessorLanguageKwarg:
    """``TextProcessor.process_macro`` forwards the resolved language."""

    @pytest.mark.asyncio
    async def test_pinned_language_resolves_to_english_display_name(self) -> None:
        processor = TextProcessor()
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///x.md",
            chunks=[ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="body", index=0)],
            language="ukr",
        )
        router = _captured_router(_EMPTY_PASS_2A_PAYLOAD)

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] == "Ukrainian"

    @pytest.mark.asyncio
    async def test_unset_language_forwards_none(self) -> None:
        processor = TextProcessor()
        doc = SourceDocument(
            source_type=SourceType.TEXT,
            source_url="file:///x.md",
            chunks=[ContentChunk(chunk_type=ChunkType.PARAGRAPH, text="body", index=0)],
            language=None,
        )
        router = _captured_router(_EMPTY_PASS_2A_PAYLOAD)

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] is None


class TestWebProcessorLanguageKwarg:
    """``WebProcessor.process_macro`` forwards the resolved language."""

    @pytest.mark.asyncio
    async def test_pinned_language_resolves_to_english_display_name(self) -> None:
        processor = WebProcessor(scrape_func=AsyncMock())
        doc = SourceDocument(
            source_type=SourceType.WEB,
            source_url="https://example.com",
            chunks=[
                ContentChunk(chunk_type=ChunkType.WEB_CONTENT, text="body", index=0)
            ],
            language="eng",
        )
        router = _captured_router(_EMPTY_PASS_2A_PAYLOAD)

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] == "English"

    @pytest.mark.asyncio
    async def test_unset_language_forwards_none(self) -> None:
        processor = WebProcessor(scrape_func=AsyncMock())
        doc = SourceDocument(
            source_type=SourceType.WEB,
            source_url="https://example.com",
            chunks=[
                ContentChunk(chunk_type=ChunkType.WEB_CONTENT, text="body", index=0)
            ],
            language=None,
        )
        router = _captured_router(_EMPTY_PASS_2A_PAYLOAD)

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] is None


# ── audio ──────────────────────────────────────────────────────────────


def _mock_redis_with_words(stt_words: list[STTWord]) -> AsyncMock:
    """ArqRedis stand-in pre-populated with serialised STT words."""
    redis = AsyncMock()
    serialised = json.dumps([w.model_dump() for w in stt_words], separators=(",", ":"))
    redis.get = AsyncMock(return_value=serialised)
    return redis


def _audio_pass_2a_json(total_words: int) -> str:
    return json.dumps(
        {
            "title": "T",
            "description": "D.",
            "segments": [
                {
                    "start_word_idx": 0,
                    "end_word_idx": total_words,
                    "title": "Seg",
                    "description": "Segment description.",
                    "main_concepts": [],
                    "secondary_concepts": [],
                    "noisy": False,
                    "subsegments": [],
                }
            ],
        }
    )


class TestAudioProcessorLanguageKwarg:
    """``AudioProcessor.process_macro`` forwards the resolved language."""

    @pytest.mark.asyncio
    async def test_pinned_language_resolves_to_english_display_name(self) -> None:
        words = [
            STTWord(text="hello", start_sec=0.0, end_sec=0.5, logprob=None),
            STTWord(text="world", start_sec=0.5, end_sec=1.0, logprob=None),
        ]
        redis = _mock_redis_with_words(words)
        processor = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="s3://b/a.mp3",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT, text="hello world", index=0
                )
            ],
            language="ukr",
        )
        router = _captured_router(_audio_pass_2a_json(total_words=2))
        set_job_from_arq(uuid.uuid4())

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] == "Ukrainian"

    @pytest.mark.asyncio
    async def test_unset_language_forwards_none(self) -> None:
        words = [
            STTWord(text="hello", start_sec=0.0, end_sec=0.5, logprob=None),
            STTWord(text="world", start_sec=0.5, end_sec=1.0, logprob=None),
        ]
        redis = _mock_redis_with_words(words)
        processor = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="s3://b/a.mp3",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT, text="hello world", index=0
                )
            ],
            language=None,
        )
        router = _captured_router(_audio_pass_2a_json(total_words=2))
        set_job_from_arq(uuid.uuid4())

        await processor.process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        assert kwargs["language"] is None


# ── video ──────────────────────────────────────────────────────────────


_JOB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


def _video_doc(words: list[SttWord], *, language: str | None) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.VIDEO,
        source_url="s3://b/v.mp4",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.TRANSCRIPT,
                text=" ".join(w.text for w in words),
                index=0,
            )
        ],
        language=language,
    )


class _CapturingStageRouter:
    """Minimal StageRouter double recording render_context kwargs."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        contents: Any = None,
        **render_context: Any,
    ) -> StageResult:
        self.calls.append({"stage": stage_name, "render_context": render_context})
        if response_validator is not None:
            response_validator(self._payload)
        return StageResult(
            content=self._payload,
            provider_used="mock",
            model_used="mock-model",
            attempt_count=1,
        )


def _video_stt_redis(words: list[SttWord]) -> AsyncMock:
    stt = SttResult(language="en", duration_ms=2000, words=words, pauses=[])
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=stt.model_dump_json())
    return redis


class TestVideoStep5LanguageKwarg:
    """``step_5_pass2a_mapping`` forwards the resolved language."""

    @pytest.mark.asyncio
    async def test_pinned_language_resolves_to_english_display_name(self) -> None:
        words = [SttWord(text="alpha", start_ms=0, end_ms=500)]
        doc = _video_doc(words, language="ukr")
        router = _CapturingStageRouter(
            json.dumps(
                {
                    "title": "T",
                    "description": "D.",
                    "segments": [
                        {
                            "start_word_idx": 0,
                            "end_word_idx": 1,
                            "title": "Seg",
                            "description": "Segment description.",
                            "main_concepts": [],
                            "secondary_concepts": [],
                            "noisy": False,
                            "subsegments": [],
                        }
                    ],
                }
            )
        )

        with patch(
            "course_supporter.ingestion.video_pipeline.steps.get_current_job_id",
            return_value=_JOB_ID,
        ):
            await steps.step_5_pass2a_mapping(
                doc, redis=_video_stt_redis(words), stage_router=router
            )

        assert router.calls
        assert router.calls[0]["render_context"]["language"] == "Ukrainian"

    @pytest.mark.asyncio
    async def test_unset_language_forwards_none(self) -> None:
        words = [SttWord(text="alpha", start_ms=0, end_ms=500)]
        doc = _video_doc(words, language=None)
        router = _CapturingStageRouter(
            json.dumps(
                {
                    "title": "T",
                    "description": "D.",
                    "segments": [
                        {
                            "start_word_idx": 0,
                            "end_word_idx": 1,
                            "title": "Seg",
                            "description": "Segment description.",
                            "main_concepts": [],
                            "secondary_concepts": [],
                            "noisy": False,
                            "subsegments": [],
                        }
                    ],
                }
            )
        )

        with patch(
            "course_supporter.ingestion.video_pipeline.steps.get_current_job_id",
            return_value=_JOB_ID,
        ):
            await steps.step_5_pass2a_mapping(
                doc, redis=_video_stt_redis(words), stage_router=router
            )

        assert router.calls
        assert router.calls[0]["render_context"]["language"] is None
