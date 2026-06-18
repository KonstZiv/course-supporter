"""Unit tests for the video media toolchain parsing (Phase 2.4 task 2.4.2).

Covers the pure ffprobe-JSON → ``VideoFileMetadata`` mapping
(``_parse_probe``) — no subprocess, no ffmpeg. The real subprocess
orchestration (download / probe / extract) runs in the
``requires_ffmpeg`` integration test.
"""

from __future__ import annotations

import pytest

from course_supporter.config import settings
from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.video_pipeline.media import (
    _DOWNLOAD_TIMEOUT_FLOOR_SEC,
    _DOWNLOAD_TIMEOUT_SEC,
    _FRAME_EXTRACT_TIMEOUT_CAP_SEC,
    _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC,
    _parse_probe,
    frame_extract_timeout_for,
)

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


class TestFrameExtractTimeout:
    """Duration-proportional frame-extraction timeout (task 3.3c-B, Krok 1)."""

    def test_short_video_clamps_to_floor(self) -> None:
        # 1-min video: linear budget (60 s) < FLOOR → fail-fast preserved.
        assert frame_extract_timeout_for(60.0) == _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC

    def test_at_floor_boundary_returns_floor(self) -> None:
        # Duration exactly equal to the FLOOR stays at the FLOOR.
        assert (
            frame_extract_timeout_for(_FRAME_EXTRACT_TIMEOUT_FLOOR_SEC)
            == _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC
        )

    def test_mid_length_video_scales_linearly(self) -> None:
        # 54-min patient-zero video (3263 s): budget 1.0 → between FLOOR/CAP.
        result = frame_extract_timeout_for(3263.0)
        assert result == pytest.approx(3263.0)
        assert result > _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC
        assert result < _FRAME_EXTRACT_TIMEOUT_CAP_SEC

    def test_contract_150min_video_well_under_cap(self) -> None:
        # 150-min contract worst case (9000 s) gets a proportional budget
        # that still sits below the CAP.
        result = frame_extract_timeout_for(9000.0)
        assert result == pytest.approx(9000.0)
        assert result < _FRAME_EXTRACT_TIMEOUT_CAP_SEC

    def test_pathological_long_video_clamps_to_cap(self) -> None:
        # 10-h out-of-contract input: clamped to CAP, never unbounded.
        assert frame_extract_timeout_for(36000.0) == _FRAME_EXTRACT_TIMEOUT_CAP_SEC

    def test_cap_stays_under_job_timeout(self) -> None:
        # Invariant: CAP + worst-case downstream must fit inside the ARQ
        # job_timeout. The CAP alone is half the budget (task 3.3c-B GO).
        assert settings.worker_job_timeout > _FRAME_EXTRACT_TIMEOUT_CAP_SEC


class TestDownloadTimeoutBudget:
    """Worst-case yt-dlp download timeout from the size cap (task 3.3c-B)."""

    def test_budget_exceeds_legacy_flat_floor(self) -> None:
        # The 5 GB cap at 1 MiB/s yields far more than the dev-era flat 600 s.
        assert _DOWNLOAD_TIMEOUT_SEC > _DOWNLOAD_TIMEOUT_FLOOR_SEC

    def test_budget_under_job_timeout(self) -> None:
        # A download alone must never consume the whole job budget.
        assert settings.worker_job_timeout > _DOWNLOAD_TIMEOUT_SEC

    def test_budget_matches_size_cap_over_min_throughput(self) -> None:
        # 5 GiB / 1 MiB/s = 5120 s (between FLOOR and CAP → operative value).
        assert pytest.approx(5120.0) == _DOWNLOAD_TIMEOUT_SEC
