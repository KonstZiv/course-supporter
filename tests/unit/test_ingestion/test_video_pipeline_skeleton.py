"""Unit tests for the video_pipeline VideoProcessor (Phase 2.4).

Krok 1-3 are real as of task 2.4.3 (ingestion + STT + detection); the
external toolchain (``media.*``, the ``stt_router``, and Krok 3's
``detection.detect``) is mocked here so the unit layer needs neither
ffmpeg nor cv2 work nor a network. Krok 4-7 remain stubs. The real
ffmpeg/cv2 detection edge runs in ``test_video_detection.py``
(``requires_ffmpeg``); the full orchestrator topology in
``tests/integration``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.ingestion.video_pipeline import VideoProcessor, steps
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    DetectionResult,
    SampledFrame,
    Scene,
    SttResult,
    VideoFileMetadata,
)
from course_supporter.models.source import ChunkType, SourceDocument, SourceType
from course_supporter.storage.orm import AuthoredDocument
from course_supporter.stt.router import STTRouter
from course_supporter.stt.schemas import STTResult, STTWord

_PKG = "course_supporter.ingestion.video_pipeline"
_DOWNLOAD = f"{_PKG}.media.download_video"
_PROBE = f"{_PKG}.media.probe_metadata"
_EXTRACT = f"{_PKG}.media.extract_audio"
_STEP3 = f"{_PKG}.steps.step_3_detection"
_STEP4 = f"{_PKG}.steps.step_4_pass1_vision"
_GET_JOB_ID = f"{_PKG}.processor.get_current_job_id"

_JOB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _meta(duration_ms: int = 60_000) -> VideoFileMetadata:
    return VideoFileMetadata(
        duration_ms=duration_ms, codec="h264", resolution="1280x720"
    )


def _detection_result() -> DetectionResult:
    """One-scene, one-frame detection stand-in for process_raw tests."""
    return DetectionResult(
        scenes=[
            Scene(
                scene_id=0,
                start_ms=0,
                end_ms=1000,
                frames=[
                    SampledFrame(
                        frame_position_ms=0,
                        change_class=ChangeClass.FIRST,
                        frame_path=Path("/tmp/x/frames/frame_000001.jpg"),
                    )
                ],
            )
        ],
        pip_mask=None,
    )


def _mock_authored_document(source_url: str = "/local/lecture.mp4") -> Mock:
    """AuthoredDocument stand-in (spec_set for attribute-access safety)."""
    doc = Mock(spec_set=AuthoredDocument)
    doc.source_url = source_url
    doc.source_type = SourceType.VIDEO
    doc.filename = "lecture.mp4"
    return doc


def _stt_router(words_sec: list[tuple[str, float, float]]) -> AsyncMock:
    """STTRouter whose ``transcribe`` returns the given word stream."""
    router = AsyncMock(spec=STTRouter)
    router.transcribe = AsyncMock(
        return_value=STTResult(
            text=" ".join(t for t, _, _ in words_sec),
            words=[STTWord(start_sec=s, end_sec=e, text=t) for t, s, e in words_sec],
            language="en",
            detected_language="en",
            provider="elevenlabs",
            model_id="scribe_v2",
        )
    )
    return router


def _make_processor(
    *,
    stt_router: AsyncMock | None = None,
    redis: AsyncMock | None = None,
) -> VideoProcessor:
    return VideoProcessor(
        stt_router=stt_router or _stt_router([("Hello", 0.0, 0.5)]),
        redis=redis or AsyncMock(),
    )


class TestVideoProcessorConstruction:
    def test_requires_stt_router_and_redis(self) -> None:
        """VideoProcessor wires with the two real-edge deps (mirror audio)."""
        proc = VideoProcessor(stt_router=Mock(), redis=Mock())
        assert isinstance(proc, VideoProcessor)


class TestStep1Ingest:
    """Krok 1 — resolve local video + ffprobe + duration cap."""

    async def test_local_path_skips_download(self) -> None:
        """An already-local (S3-resolved) path goes straight to ffprobe."""
        meta = _meta()
        source = _mock_authored_document("/tmp/seed/lecture.mp4")

        with (
            patch(_DOWNLOAD) as dl,
            patch(_PROBE, new=AsyncMock(return_value=meta)),
        ):
            video_path, out_meta = await steps.step_1_ingest(source, tmp=Path("/tmp/x"))

        dl.assert_not_called()
        assert video_path == Path("/tmp/seed/lecture.mp4")
        assert out_meta is meta

    async def test_url_path_downloads_video_stream(self) -> None:
        """An http(s) URL is downloaded via yt-dlp before ffprobe."""
        meta = _meta()
        downloaded = Path("/tmp/x/source.mp4")
        source = _mock_authored_document("https://youtube.com/watch?v=abc")

        with (
            patch(_DOWNLOAD, new=AsyncMock(return_value=downloaded)) as dl,
            patch(_PROBE, new=AsyncMock(return_value=meta)),
        ):
            video_path, _ = await steps.step_1_ingest(source, tmp=Path("/tmp/x"))

        dl.assert_awaited_once_with("https://youtube.com/watch?v=abc", Path("/tmp/x"))
        assert video_path == downloaded

    async def test_duration_cap_raises_unsupported_format(self) -> None:
        """Over-150-min video is rejected before any STT cost (constraint)."""
        over = _meta(151 * 60 * 1000)
        source = _mock_authored_document()

        with (
            patch(_DOWNLOAD),
            patch(_PROBE, new=AsyncMock(return_value=over)),
            pytest.raises(UnsupportedFormatError, match="exceeds maximum"),
        ):
            await steps.step_1_ingest(source, tmp=Path("/tmp/x"))


class TestStep2Stt:
    """Krok 2 — audio extraction + Scribe + sec→ms + pause derivation."""

    async def test_converts_seconds_to_ms_and_derives_pauses(self) -> None:
        """STT seconds → §1 ms words; a >=700ms gap becomes a pause."""
        meta = _meta(9_000)
        # gap word1.end=0.5s -> word2.start=1.4s == 900ms >= threshold.
        router = _stt_router([("Hello", 0.0, 0.5), ("world", 1.4, 2.0)])

        with patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))):
            result = await steps.step_2_stt(
                Path("/tmp/x/source.mp4"), meta, stt_router=router, tmp=Path("/tmp/x")
            )

        assert [w.text for w in result.words] == ["Hello", "world"]
        assert (result.words[0].start_ms, result.words[0].end_ms) == (0, 500)
        assert (result.words[1].start_ms, result.words[1].end_ms) == (1400, 2000)
        assert result.duration_ms == 9_000  # from ffprobe, not STT
        assert len(result.pauses) == 1
        assert (result.pauses[0].start_ms, result.pauses[0].end_ms) == (500, 1400)
        assert result.detected_language == "en"

    async def test_short_gap_yields_no_pause(self) -> None:
        """A sub-threshold inter-word gap is not a boundary candidate."""
        meta = _meta(2_000)
        router = _stt_router([("Hello", 0.0, 0.5), ("world", 0.6, 1.0)])  # 100ms gap

        with patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))):
            result = await steps.step_2_stt(
                Path("/tmp/x/source.mp4"), meta, stt_router=router, tmp=Path("/tmp/x")
            )

        assert result.pauses == []


class TestProcessRawPipeline:
    """process_raw chains Krok 1-4 + writes the Redis STT carrier."""

    async def test_builds_video_source_document(self) -> None:
        meta = _meta()
        proc = _make_processor(stt_router=_stt_router([("Hello", 0.0, 0.5)]))

        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_PROBE, new=AsyncMock(return_value=meta)),
            patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))),
            patch(_STEP3, new=AsyncMock(return_value=_detection_result())),
        ):
            doc = await proc.process_raw(_mock_authored_document())

        assert isinstance(doc, SourceDocument)
        assert doc.source_type == SourceType.VIDEO
        chunk_types = {c.chunk_type for c in doc.chunks}
        assert ChunkType.TRANSCRIPT in chunk_types
        assert ChunkType.VISUAL_SCENE in chunk_types
        assert doc.metadata["detected_language"] == "en"

    async def test_writes_stt_carrier_to_redis_in_readable_form(self) -> None:
        """Acceptance #4 — Redis payload round-trips via SttResult."""
        meta = _meta()
        redis = AsyncMock()
        proc = _make_processor(
            stt_router=_stt_router([("Hello", 0.0, 0.5), ("world", 0.6, 1.0)]),
            redis=redis,
        )

        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_PROBE, new=AsyncMock(return_value=meta)),
            patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))),
            patch(_STEP3, new=AsyncMock(return_value=_detection_result())),
        ):
            await proc.process_raw(_mock_authored_document())

        redis.set.assert_awaited_once()
        call = redis.set.await_args
        assert call.args[0] == f"video_stt_result:{_JOB_ID}"
        assert call.kwargs["ex"] == 3600
        carrier = SttResult.model_validate_json(call.args[1])
        assert [w.text for w in carrier.words] == ["Hello", "world"]
        assert carrier.duration_ms == 60_000

    async def test_missing_job_id_raises(self) -> None:
        proc = _make_processor()
        with (
            patch(_GET_JOB_ID, return_value=None),
            pytest.raises(ProcessingError, match="ContextVar job_id"),
        ):
            await proc.process_raw(_mock_authored_document())


class TestErrorTaxonomy:
    """Acceptance #5 — distinct exception classes per failure category."""

    async def test_download_failure_is_processing_error(self) -> None:
        """Operational: yt-dlp failure surfaces as ProcessingError."""
        proc = _make_processor()
        source = _mock_authored_document("https://youtube.com/watch?v=abc")
        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_DOWNLOAD, new=AsyncMock(side_effect=ProcessingError("yt-dlp boom"))),
            pytest.raises(ProcessingError, match="yt-dlp boom"),
        ):
            await proc.process_raw(source)

    async def test_cap_is_unsupported_format_error(self) -> None:
        """Constraint: over-cap duration surfaces as UnsupportedFormatError."""
        over = _meta(200 * 60 * 1000)
        proc = _make_processor()
        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_PROBE, new=AsyncMock(return_value=over)),
            pytest.raises(UnsupportedFormatError),
        ):
            await proc.process_raw(_mock_authored_document())

    async def test_stt_failure_propagates(self) -> None:
        """Operational: STT provider failure propagates out of process_raw."""
        meta = _meta()
        router = AsyncMock(spec=STTRouter)
        router.transcribe = AsyncMock(side_effect=ProcessingError("scribe down"))
        proc = _make_processor(stt_router=router)
        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_PROBE, new=AsyncMock(return_value=meta)),
            patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))),
            pytest.raises(ProcessingError, match="scribe down"),
        ):
            await proc.process_raw(_mock_authored_document())


class TestFailureInjectionSeam:
    """A downstream stub gnízdo can still be patched to raise (R1 seam)."""

    async def test_step4_failure_propagates(self) -> None:
        meta = _meta()
        proc = _make_processor()
        with (
            patch(_GET_JOB_ID, return_value=_JOB_ID),
            patch(_PROBE, new=AsyncMock(return_value=meta)),
            patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))),
            patch(_STEP3, new=AsyncMock(return_value=_detection_result())),
            patch(_STEP4, new=AsyncMock(side_effect=ProcessingError("vision boom"))),
            pytest.raises(ProcessingError, match="vision boom"),
        ):
            await proc.process_raw(_mock_authored_document())


def _doc_with_text(text: str) -> SourceDocument:
    from course_supporter.models.source import ContentChunk

    return SourceDocument(
        source_type=SourceType.VIDEO,
        source_url="s3://bucket/lecture.mp4",
        title="lecture.mp4",
        chunks=[ContentChunk(chunk_type=ChunkType.TRANSCRIPT, text=text, index=0)],
    )


class TestMacroAndDetailStubs:
    """Krok 5-7 stubs still produce a valid contiguous cover (unchanged)."""

    async def test_macro_returns_contiguous_cover(self) -> None:
        proc = _make_processor()
        doc = _doc_with_text("some reasonably long mock transcript text here")

        summary = await proc.process_macro(doc, Mock())

        assert isinstance(summary, DocumentSummaryDraft)
        assert summary.segments[0].start_pos == 0
        assert summary.segments[-1].end_pos == len(doc.assemble_text())
        assert summary.segments[-1].noisy is True

    async def test_detail_fills_and_cleans(self) -> None:
        proc = _make_processor()
        doc = _doc_with_text("some reasonably long mock transcript text here")
        summary = await proc.process_macro(doc, Mock())

        detail = await proc.process_detail(doc, summary)

        assert all(isinstance(s, DocumentSegmentDraft) for s in detail)
        assert all(s.content for s in detail)
        assert any(
            (s.content or "").startswith("[cleaned] ") for s in detail if s.noisy
        )


class TestVideoPipelineIsolation:
    """Acceptance #3/#8 — namespace has zero ``course_supporter.vd`` imports."""

    def test_no_vd_imports_in_namespace(self) -> None:
        import course_supporter.ingestion.video_pipeline as pkg

        package_dir = Path(pkg.__file__).parent
        import_forms = ("from course_supporter.vd", "import course_supporter.vd")
        offenders = [
            py.name
            for py in sorted(package_dir.glob("*.py"))
            if any(form in py.read_text(encoding="utf-8") for form in import_forms)
        ]
        assert offenders == [], (
            f"video_pipeline modules must not import course_supporter.vd; "
            f"offenders: {offenders}"
        )
