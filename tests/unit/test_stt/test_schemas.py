"""Tests for STT schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment


class TestSTTRequest:
    def test_defaults(self) -> None:
        req = STTRequest(audio_path="/tmp/audio.mp3")
        assert req.language is None
        assert req.action == "transcribe"
        assert req.strategy == "default"
        assert req.duration_sec is None

    def test_with_duration_sec(self) -> None:
        req = STTRequest(audio_path="/tmp/a.mp3", duration_sec=9000.0)
        assert req.duration_sec == 9000.0

    def test_with_language(self) -> None:
        req = STTRequest(audio_path="/tmp/a.wav", language="uk")
        assert req.language == "uk"

    def test_invalid_language_rejected(self) -> None:
        with pytest.raises(ValidationError):
            STTRequest(audio_path="/tmp/a.wav", language="UKR")

    def test_invalid_language_too_long(self) -> None:
        with pytest.raises(ValidationError):
            STTRequest(audio_path="/tmp/a.wav", language="ukr")


class TestSTTSegment:
    def test_basic(self) -> None:
        seg = STTSegment(start_sec=0.0, end_sec=15.5, text="Hello")
        assert seg.start_sec == 0.0
        assert seg.end_sec == 15.5
        assert seg.text == "Hello"


class TestSTTResult:
    def test_minimal(self) -> None:
        r = STTResult(text="hello", provider="test", model_id="m1")
        assert r.text == "hello"
        assert r.segments == []
        assert r.language is None
        assert r.confidence is None
        assert r.latency_ms == 0
        assert r.cost_usd is None
        assert r.action == ""
        assert r.strategy == "default"

    def test_with_all_fields(self) -> None:
        now = datetime.now(UTC)
        r = STTResult(
            text="transcript",
            segments=[STTSegment(start_sec=0, end_sec=10, text="seg")],
            language="uk",
            confidence=0.95,
            provider="elevenlabs",
            model_id="scribe_v1",
            latency_ms=500,
            audio_duration_sec=635.0,
            cost_usd=0.039,
            action="transcribe",
            strategy="quality",
            finished_at=now,
        )
        assert r.provider == "elevenlabs"
        assert len(r.segments) == 1
        assert r.finished_at == now
