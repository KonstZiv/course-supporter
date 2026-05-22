"""Unit tests for the video media toolchain parsing (Phase 2.4 task 2.4.2).

Covers the pure ffprobe-JSON → ``VideoFileMetadata`` mapping
(``_parse_probe``) — no subprocess, no ffmpeg. The real subprocess
orchestration (download / probe / extract) runs in the
``requires_ffmpeg`` integration test.
"""

from __future__ import annotations

import pytest

from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.video_pipeline.media import _parse_probe

_PROBE_OK = {
    "format": {"duration": "61.5"},
    "streams": [
        {"codec_type": "audio", "codec_name": "aac"},
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
    ],
}


class TestParseProbe:
    def test_maps_duration_codec_resolution(self) -> None:
        meta = _parse_probe(_PROBE_OK, source_name="lecture.mp4")

        assert meta.duration_ms == 61_500
        assert meta.codec == "h264"
        assert meta.resolution == "1920x1080"

    def test_duration_falls_back_to_stream(self) -> None:
        data = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "vp9",
                    "width": 640,
                    "height": 480,
                    "duration": "12.0",
                }
            ],
        }
        meta = _parse_probe(data, source_name="x.webm")

        assert meta.duration_ms == 12_000
        assert meta.codec == "vp9"

    def test_no_video_stream_raises(self) -> None:
        data = {"format": {"duration": "10"}, "streams": [{"codec_type": "audio"}]}
        with pytest.raises(UnsupportedFormatError, match="No video stream"):
            _parse_probe(data, source_name="audio_only.mkv")

    def test_missing_duration_raises(self) -> None:
        data = {
            "format": {},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        }
        with pytest.raises(UnsupportedFormatError, match="determine duration"):
            _parse_probe(data, source_name="corrupt.mp4")

    def test_unknown_resolution_when_dimensions_missing(self) -> None:
        data = {
            "format": {"duration": "5"},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        }
        meta = _parse_probe(data, source_name="nores.mp4")

        assert meta.resolution == "unknown"
