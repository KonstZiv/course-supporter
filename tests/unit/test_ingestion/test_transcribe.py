"""Tests for local_transcribe heavy step."""

from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import (
    TranscribeParams,
    Transcript,
    TranscriptSegment,
    WhisperModelSize,
)
from course_supporter.ingestion.transcribe import local_transcribe


def _mock_whisper(
    segments: list[dict[str, object]],
    language: str | None = "en",
) -> MagicMock:
    """Create a mock whisper module with pre-configured model."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {
        "segments": segments,
        "language": language,
    }
    mock_module = MagicMock()
    mock_module.load_model.return_value = mock_model
    return mock_module


class TestLocalTranscribeSuccess:
    async def test_returns_transcript(self) -> None:
        """Produces Transcript with segments from Whisper output."""
        mock = _mock_whisper(
            [
                {"start": 0.0, "end": 10.5, "text": "Hello world"},
                {"start": 10.5, "end": 20.0, "text": "Second segment"},
            ],
            language="en",
        )
        with patch.dict("sys.modules", {"whisper": mock}):
            result = await local_transcribe("/tmp/audio.wav", TranscribeParams())

        assert isinstance(result, Transcript)
        assert len(result.segments) == 2
        assert result.language == "en"

    async def test_segment_timestamps(self) -> None:
        """Segments carry rounded start_sec and end_sec."""
        mock = _mock_whisper(
            [{"start": 1.12345, "end": 5.6789, "text": "speech"}],
            language="uk",
        )
        with patch.dict("sys.modules", {"whisper": mock}):
            result = await local_transcribe("/tmp/a.wav", TranscribeParams())

        seg = result.segments[0]
        assert isinstance(seg, TranscriptSegment)
        assert seg.start_sec == 1.12
        assert seg.end_sec == 5.68
        assert seg.text == "speech"

    async def test_uses_model_name_param(self) -> None:
        """Passes model_name from params to whisper.load_model."""
        mock = _mock_whisper([], language=None)
        params = TranscribeParams(model_name=WhisperModelSize.LARGE)

        with patch.dict("sys.modules", {"whisper": mock}):
            await local_transcribe("/tmp/a.wav", params)

        mock.load_model.assert_called_once_with("large")

    async def test_passes_language_to_whisper(self) -> None:
        """When language is set, passes it to model.transcribe."""
        mock = _mock_whisper([], language="uk")
        params = TranscribeParams(language="uk")

        with patch.dict("sys.modules", {"whisper": mock}):
            await local_transcribe("/tmp/a.wav", params)

        model = mock.load_model.return_value
        # transcribe is called via lambda, check the actual call
        call_args = model.transcribe.call_args
        assert call_args[0][0] == "/tmp/a.wav"
        assert call_args[1] == {"language": "uk"}

    async def test_no_language_omits_kwarg(self) -> None:
        """When language is None, does not pass it to model.transcribe."""
        mock = _mock_whisper([], language=None)

        with patch.dict("sys.modules", {"whisper": mock}):
            await local_transcribe("/tmp/a.wav", TranscribeParams())

        model = mock.load_model.return_value
        call_args = model.transcribe.call_args
        assert call_args[0][0] == "/tmp/a.wav"
        assert call_args[1] == {}


class TestLocalTranscribeEdgeCases:
    async def test_empty_segments(self) -> None:
        """Empty Whisper result produces empty Transcript."""
        mock = _mock_whisper([], language=None)

        with patch.dict("sys.modules", {"whisper": mock}):
            result = await local_transcribe("/tmp/a.wav", TranscribeParams())

        assert result.segments == []
        assert result.language is None

    async def test_whitespace_only_segments_skipped(self) -> None:
        """Segments with whitespace-only text are filtered out."""
        mock = _mock_whisper(
            [
                {"start": 0.0, "end": 5.0, "text": "   "},
                {"start": 5.0, "end": 10.0, "text": "actual"},
                {"start": 10.0, "end": 15.0, "text": ""},
            ],
            language="en",
        )

        with patch.dict("sys.modules", {"whisper": mock}):
            result = await local_transcribe("/tmp/a.wav", TranscribeParams())

        assert len(result.segments) == 1
        assert result.segments[0].text == "actual"

    async def test_detected_language_passed_through(self) -> None:
        """Whisper detected language is returned in Transcript."""
        mock = _mock_whisper(
            [{"start": 0.0, "end": 1.0, "text": "тест"}],
            language="uk",
        )

        with patch.dict("sys.modules", {"whisper": mock}):
            result = await local_transcribe("/tmp/a.wav", TranscribeParams())

        assert result.language == "uk"


class TestLocalTranscribeErrors:
    async def test_whisper_not_installed(self) -> None:
        """When whisper package is not installed, raises ProcessingError."""
        with (
            patch.dict("sys.modules", {"whisper": None}),
            pytest.raises(ProcessingError, match="whisper is not installed"),
        ):
            await local_transcribe("/tmp/a.wav", TranscribeParams())


class TestTranscribeParamsValidation:
    def test_default_params(self) -> None:
        """Default params use base model and no language."""
        params = TranscribeParams()
        assert params.model_name == WhisperModelSize.BASE
        assert params.language is None

    def test_valid_language(self) -> None:
        """Valid ISO 639-1 code is accepted."""
        params = TranscribeParams(language="uk")
        assert params.language == "uk"

    def test_invalid_language_rejected(self) -> None:
        """Non ISO 639-1 code is rejected."""
        with pytest.raises(ValueError, match="String should match pattern"):
            TranscribeParams(language="eng")

    def test_invalid_model_rejected(self) -> None:
        """Unknown model name is rejected."""
        with pytest.raises(ValueError):
            TranscribeParams(model_name="nonexistent")  # type: ignore[arg-type]
