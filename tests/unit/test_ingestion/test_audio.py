"""Unit tests for AudioProcessor three-stage pipeline (KD-2.2-D/E/F/A/I)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from course_supporter.ingestion.audio import (
    AudioProcessor,
    chars_per_word_cumsum,
)
from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import (
    AudioPass2aResult,
    AudioSegmentDraft,
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.service_logging import set_job_from_arq
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.stt.schemas import STTResult, STTSegment, STTWord

if TYPE_CHECKING:
    pass


# ──────────────────────────────────────────────────────────────────────
# Helpers — minimal, inline scope per Rule #29 boundary (no conftest).
# ──────────────────────────────────────────────────────────────────────


def _word(text: str, start: float, end: float) -> STTWord:
    return STTWord(text=text, start_sec=start, end_sec=end)


def _stt_result(
    words: list[STTWord], segments: list[STTSegment] | None = None
) -> STTResult:
    """Build STTResult mirroring Scribe v2 word-level alignment."""
    if segments is None and words:
        # default: single segment covering all words
        segments = [
            STTSegment(
                start_sec=words[0].start_sec,
                end_sec=words[-1].end_sec,
                text=" ".join(w.text for w in words),
            )
        ]
    return STTResult(
        text=" ".join(w.text for w in words),
        segments=segments or [],
        words=words,
        provider="mock-scribe",
        model_id="mock-scribe-v2",
    )


def _mock_stt_router(result: STTResult) -> AsyncMock:
    """AsyncMock STTRouter parallel to test_video.py:175 precedent."""
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
    redis._store = store
    return redis


def _mock_authored_document(
    *,
    source_type: SourceType = SourceType.AUDIO,
    source_url: str = "/tmp/audio.mp3",
    filename: str | None = "audio.mp3",
) -> Mock:
    """AuthoredDocument stand-in with spec_set for attribute-access safety."""
    doc = Mock(spec_set=AuthoredDocument)
    doc.source_type = source_type
    doc.source_url = source_url
    doc.filename = filename
    return doc


def _mock_stage_router_for_pass2a(result: AudioPass2aResult) -> AsyncMock:
    """StageRouter mock that drives Pass 2a response_validator with JSON content."""
    router = AsyncMock()

    async def _execute(
        stage: str,
        *,
        response_validator: Callable[[str], None],
        **template_kwargs: object,
    ) -> MagicMock:
        # Audio process_macro invokes response_validator with serialized JSON;
        # mock returns the AudioPass2aResult marshalled in JSON form so the
        # validator parses + populates the parsed[] capture closure.
        json_content = result.model_dump_json()
        response_validator(json_content)
        out = MagicMock()
        out.provider_used = "mock-llm"
        out.model_used = "mock-model"
        out.attempt_count = 1
        return out

    router.execute_for_stage.side_effect = _execute
    return router


def _mock_stage_router_for_pass2c(cleaned: str) -> AsyncMock:
    """StageRouter mock that drives Pass 2c response_validator with cleaned text."""
    router = AsyncMock()

    async def _execute(
        stage: str,
        *,
        response_validator: Callable[[str], None],
        **template_kwargs: object,
    ) -> MagicMock:
        response_validator(cleaned)
        out = MagicMock()
        out.provider_used = "mock-llm"
        out.model_used = "mock-model"
        out.attempt_count = 1
        return out

    router.execute_for_stage.side_effect = _execute
    return router


# ──────────────────────────────────────────────────────────────────────
# chars_per_word_cumsum bridge helper (KD-2.2-F).
# ──────────────────────────────────────────────────────────────────────


class TestCharsPerWordCumsum:
    """KD-2.2-F bridge invariants — word-index → char-offset mapping."""

    def test_empty_words_returns_single_zero(self) -> None:
        assert chars_per_word_cumsum([]) == [0]

    def test_single_word_no_trailing_space(self) -> None:
        """Last word does not carry a trailing separator."""
        words = [_word("Hello", 0.0, 0.5)]
        # cumsum = [0, len("Hello")] — no +1 for space (last word)
        assert chars_per_word_cumsum(words) == [0, 5]

    def test_multiple_words_inter_word_spaces(self) -> None:
        """Every non-last word contributes its length + 1 (trailing space)."""
        words = [
            _word("Hi", 0.0, 0.3),
            _word("Bob", 0.3, 0.6),
            _word("ok", 0.6, 0.9),
        ]
        # cumsum = [0, 2+1=3, 3+3+1=7, 7+2=9]
        assert chars_per_word_cumsum(words) == [0, 3, 7, 9]

    def test_last_entry_matches_assemble_text_length(self) -> None:
        """Bridge invariant — last cumsum entry == len(" ".join(...))."""
        words = [_word("foo", 0.0, 0.2), _word("bar", 0.2, 0.4)]
        cumsum = chars_per_word_cumsum(words)
        assert cumsum[-1] == len(" ".join(w.text for w in words))


# ──────────────────────────────────────────────────────────────────────
# Inline Redis cache helpers (_store_words / _fetch_words; KD-2.2-D).
# ──────────────────────────────────────────────────────────────────────


class TestWordCache:
    """KD-2.2-D inline Redis cache — round-trip + miss semantics."""

    async def test_store_then_fetch_round_trip(self) -> None:
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        job_id = uuid.uuid4()
        words = [_word("foo", 0.0, 0.2), _word("bar", 0.2, 0.4)]

        await proc._store_words(job_id, words)
        recovered = await proc._fetch_words(job_id)

        assert recovered is not None
        assert [w.text for w in recovered] == ["foo", "bar"]
        assert [w.start_sec for w in recovered] == [0.0, 0.2]

    async def test_fetch_miss_returns_none(self) -> None:
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        assert await proc._fetch_words(uuid.uuid4()) is None

    async def test_store_uses_ttl(self) -> None:
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        await proc._store_words(uuid.uuid4(), [_word("x", 0.0, 0.1)])

        # _set side_effect was async; verify Redis was called with ex=3600
        assert redis.set.call_args.kwargs.get("ex") == 3600


# ──────────────────────────────────────────────────────────────────────
# AudioProcessor.process_raw (Stage 1).
# ──────────────────────────────────────────────────────────────────────


class TestProcessRaw:
    """KD-2.2-D/E — STT + cache + chunks + critical invariant assert."""

    async def test_happy_path_chunks_and_cache(self) -> None:
        words = [
            _word("Hello", 0.0, 0.5),
            _word("world", 0.5, 1.0),
        ]
        segments = [STTSegment(start_sec=0.0, end_sec=1.0, text="Hello world")]
        result = _stt_result(words, segments)
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=_mock_stt_router(result), redis=redis)
        set_job_from_arq(uuid.uuid4())

        doc = await proc.process_raw(_mock_authored_document())

        assert doc.source_type == SourceType.AUDIO
        assert len(doc.chunks) == 1
        assert doc.chunks[0].chunk_type == ChunkType.TRANSCRIPT
        assert doc.chunks[0].text == "Hello world"
        # cache populated
        assert len(redis._store) == 1

    async def test_entry_guard_empty_words_raises(self) -> None:
        """KD-2.2-C — provider must surface word-level alignment."""
        result = STTResult(
            text="",
            segments=[],
            words=[],
            provider="bad",
            model_id="bad-model",
        )
        proc = AudioProcessor(stt_router=_mock_stt_router(result), redis=_mock_redis())
        set_job_from_arq(uuid.uuid4())

        with pytest.raises(ProcessingError, match=r"empty\s+words"):
            await proc.process_raw(_mock_authored_document())

    async def test_context_var_missing_raises(self, monkeypatch) -> None:
        """process_raw requires ContextVar job_id (arq task entry contract)."""
        words = [_word("Hi", 0.0, 0.3)]
        proc = AudioProcessor(
            stt_router=_mock_stt_router(_stt_result(words)),
            redis=_mock_redis(),
        )
        monkeypatch.setattr(
            "course_supporter.ingestion.audio.get_current_job_id",
            lambda: None,
        )

        with pytest.raises(ProcessingError, match="ContextVar"):
            await proc.process_raw(_mock_authored_document())

    async def test_critical_invariant_assert_holds(self) -> None:
        """KD-2.2-E line 333 — assemble_text == ' '.join(words)."""
        words = [
            _word("foo", 0.0, 0.3),
            _word("bar", 0.3, 0.6),
            _word("baz", 0.6, 0.9),
        ]
        segments = [STTSegment(start_sec=0.0, end_sec=0.9, text="foo bar baz")]
        proc = AudioProcessor(
            stt_router=_mock_stt_router(_stt_result(words, segments)),
            redis=_mock_redis(),
        )
        set_job_from_arq(uuid.uuid4())

        doc = await proc.process_raw(_mock_authored_document())

        expected = " ".join(w.text for w in words)
        assert doc.assemble_text() == expected


# ──────────────────────────────────────────────────────────────────────
# AudioProcessor.process_macro (Stage 2 — Pass 2a).
# ──────────────────────────────────────────────────────────────────────


class TestProcessMacro:
    """KD-2.2-A + KD-2.2-H + KD-2.2-F + KD-2.2-O concept aggregation."""

    @staticmethod
    def _pass2a_result(
        *,
        title: str | None,
        description: str,
        segments: list[AudioSegmentDraft],
    ) -> AudioPass2aResult:
        return AudioPass2aResult(
            title=title,
            description=description,
            segments=segments,
        )

    @staticmethod
    def _seg(
        start: int,
        end: int,
        *,
        main: list[str] | None = None,
        secondary: list[str] | None = None,
        noisy: bool = False,
    ) -> AudioSegmentDraft:
        return AudioSegmentDraft(
            start_word_idx=start,
            end_word_idx=end,
            description="d",
            main_concepts=main or [],
            secondary_concepts=secondary or [],
            noisy=noisy,
        )

    async def _prepare(
        self, words: list[STTWord], pass2a: AudioPass2aResult
    ) -> tuple[AudioProcessor, SourceDocument, AsyncMock]:
        redis = _mock_redis()
        stt_router = _mock_stt_router(_stt_result(words))
        proc = AudioProcessor(stt_router=stt_router, redis=redis)
        job_id = uuid.uuid4()
        set_job_from_arq(job_id)

        # Drive process_raw end-to-end: populates the word-cache (process_macro
        # _fetch_words consumer) AND yields the canonical SourceDocument the
        # production pipeline would hand off to process_macro. Using this exact
        # instance guards against drift between process_raw's actual output
        # shape and a manually-reconstructed test stand-in.
        doc = await proc.process_raw(_mock_authored_document())
        stage_router = _mock_stage_router_for_pass2a(pass2a)
        return proc, doc, stage_router

    async def test_three_pass_through_fields(self) -> None:
        """Title + description + segment-level noisy → DocumentSummaryDraft."""
        words = [_word(f"w{i}", i * 0.1, (i + 1) * 0.1) for i in range(4)]
        pass2a = self._pass2a_result(
            title="Audio Title",
            description="Audio description here.",
            segments=[self._seg(0, 4, noisy=True)],
        )
        proc, doc, stage_router = await self._prepare(words, pass2a)

        summary = await proc.process_macro(doc, stage_router)

        assert summary.title == "Audio Title"
        assert summary.description == "Audio description here."
        assert summary.segments[0].noisy is True

    async def test_title_none_becomes_empty_string(self) -> None:
        """None title sentinel — DocumentSummaryDraft.title must be str."""
        words = [_word(f"w{i}", i * 0.1, (i + 1) * 0.1) for i in range(2)]
        pass2a = self._pass2a_result(
            title=None,
            description="d",
            segments=[self._seg(0, 2)],
        )
        proc, doc, stage_router = await self._prepare(words, pass2a)

        summary = await proc.process_macro(doc, stage_router)

        assert summary.title == ""

    async def test_concept_aggregation_conflict_rule(self) -> None:
        """Conflict rule: main wins over secondary (parallel to KD-2.1-O)."""
        words = [_word(f"w{i}", i * 0.1, (i + 1) * 0.1) for i in range(4)]
        pass2a = self._pass2a_result(
            title="t",
            description="d",
            segments=[
                self._seg(0, 2, main=["X", "Y"], secondary=["Z"]),
                self._seg(2, 4, main=["A"], secondary=["X"]),
            ],
        )
        proc, doc, stage_router = await self._prepare(words, pass2a)

        summary = await proc.process_macro(doc, stage_router)

        assert summary.main_concepts == sorted(["A", "X", "Y"])
        # X promoted to main in seg 2 -> removed from secondary aggregation
        assert summary.secondary_concepts == ["Z"]

    async def test_cumsum_bridge_offsets_populated(self) -> None:
        """KD-2.2-F bridge — start_pos / end_pos derived from chars_per_word_cumsum."""
        words = [
            _word("Hi", 0.0, 0.3),
            _word("Bob", 0.3, 0.6),
            _word("ok", 0.6, 0.9),
        ]
        pass2a = self._pass2a_result(
            title="t",
            description="d",
            segments=[self._seg(0, 3)],
        )
        proc, doc, stage_router = await self._prepare(words, pass2a)

        summary = await proc.process_macro(doc, stage_router)

        seg = summary.segments[0]
        # cumsum = [0, 3, 7, 9]
        assert seg.start_pos == 0
        assert seg.end_pos == 9
        assert seg.start_time_sec == 0.0
        assert seg.end_time_sec == 0.9

    async def test_cache_miss_raises(self) -> None:
        """Cache miss → ProcessingError (process_raw must run first)."""
        redis = _mock_redis()
        proc = AudioProcessor(stt_router=AsyncMock(), redis=redis)
        set_job_from_arq(uuid.uuid4())
        doc = SourceDocument(
            source_type=SourceType.AUDIO, source_url="/tmp/x.mp3", chunks=[]
        )
        stage_router = AsyncMock()

        with pytest.raises(ProcessingError, match="word cache miss"):
            await proc.process_macro(doc, stage_router)


# ──────────────────────────────────────────────────────────────────────
# AudioProcessor.process_detail (Stage 3 — Pass 2b + Pass 2c).
# ──────────────────────────────────────────────────────────────────────


class TestProcessDetail:
    """KD-2.2-F (Pass 2b slice) + KD-2.2-I (Pass 2c selective routing)."""

    @staticmethod
    def _draft(
        order: int,
        start: int,
        end: int,
        *,
        noisy: bool = False,
    ) -> DocumentSegmentDraft:
        return DocumentSegmentDraft(
            order=order,
            start_pos=start,
            end_pos=end,
            description="d",
            noisy=noisy,
        )

    async def test_pass_2b_slice_always_runs(self) -> None:
        """Every content=None draft receives raw slice; non-noisy keeps slice."""
        proc = AudioProcessor(stt_router=AsyncMock(), redis=_mock_redis())
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="/tmp/x.mp3",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT, text="hello world foobar"
                ),
            ],
        )
        # Contiguous segments per DocumentSummaryDraft validator: first.start=0,
        # prev.end == next.start; non-last includes trailing inter-word space.
        summary = DocumentSummaryDraft(
            title="t",
            description="d",
            segments=[
                self._draft(0, 0, 6, noisy=False),
                self._draft(1, 6, 18, noisy=False),
            ],
        )

        result = await proc.process_detail(doc, summary)

        assert result[0].content == "hello "
        assert result[1].content == "world foobar"

    async def test_pass_2c_noisy_true_invokes_denoise(self) -> None:
        """KD-2.2-I — noisy=True segment routes through Pass 2c denoise."""
        proc = AudioProcessor(stt_router=AsyncMock(), redis=_mock_redis())
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="/tmp/x.mp3",
            chunks=[ContentChunk(chunk_type=ChunkType.TRANSCRIPT, text="raw noisy")],
        )
        summary = DocumentSummaryDraft(
            title="t",
            description="d",
            segments=[self._draft(0, 0, 9, noisy=True)],
        )
        stage_router = _mock_stage_router_for_pass2c("cleaned noisy")

        result = await proc.process_detail(doc, summary, router=stage_router)

        assert result[0].content == "cleaned noisy"
        stage_router.execute_for_stage.assert_awaited_once()

    async def test_pass_2c_noisy_false_skips_denoise(self) -> None:
        """KD-2.2-I — noisy=False segment uses raw slice, no Pass 2c call."""
        proc = AudioProcessor(stt_router=AsyncMock(), redis=_mock_redis())
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="/tmp/x.mp3",
            chunks=[ContentChunk(chunk_type=ChunkType.TRANSCRIPT, text="hello world")],
        )
        summary = DocumentSummaryDraft(
            title="t",
            description="d",
            segments=[self._draft(0, 0, 5, noisy=False)],
        )
        stage_router = _mock_stage_router_for_pass2c("WILL_NOT_BE_USED")

        result = await proc.process_detail(doc, summary, router=stage_router)

        assert result[0].content == "hello"
        stage_router.execute_for_stage.assert_not_called()

    async def test_router_none_skips_pass2c_with_warning(self) -> None:
        """Activation deferral fallback — router=None skips Pass 2c."""
        proc = AudioProcessor(stt_router=AsyncMock(), redis=_mock_redis())
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="/tmp/x.mp3",
            chunks=[ContentChunk(chunk_type=ChunkType.TRANSCRIPT, text="raw noisy")],
        )
        summary = DocumentSummaryDraft(
            title="t",
            description="d",
            segments=[self._draft(0, 0, 9, noisy=True)],
        )

        result = await proc.process_detail(doc, summary)

        # noisy=True but no router → raw slice kept
        assert result[0].content == "raw noisy"

    async def test_pass_2c_routes_only_noisy_segments(self) -> None:
        """Mixed noisy+clean segments: Pass 2c invoked once per noisy."""
        proc = AudioProcessor(stt_router=AsyncMock(), redis=_mock_redis())
        doc = SourceDocument(
            source_type=SourceType.AUDIO,
            source_url="/tmp/x.mp3",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text="part1 part2 part3 part4",
                )
            ],
        )
        # Contiguous (0,6),(6,12),(12,18),(18,23) — trailing-space inclusion
        # for non-last segments parallels chars_per_word_cumsum bridge contract.
        summary = DocumentSummaryDraft(
            title="t",
            description="d",
            segments=[
                self._draft(0, 0, 6, noisy=False),
                self._draft(1, 6, 12, noisy=True),
                self._draft(2, 12, 18, noisy=False),
                self._draft(3, 18, 23, noisy=True),
            ],
        )
        stage_router = _mock_stage_router_for_pass2c("cleaned")

        result = await proc.process_detail(doc, summary, router=stage_router)

        # only segments 1 and 3 (noisy=True) replaced with cleaned
        assert result[0].content == "part1 "
        assert result[1].content == "cleaned"
        assert result[2].content == "part3 "
        assert result[3].content == "cleaned"
        assert stage_router.execute_for_stage.await_count == 2
