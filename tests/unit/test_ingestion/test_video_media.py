"""Unit tests for the video media toolchain parsing (Phase 2.4 task 2.4.2).

Covers the pure ffprobe-JSON → ``VideoFileMetadata`` mapping
(``_parse_probe``) — no subprocess, no ffmpeg. The real subprocess
orchestration (download / probe / extract) runs in the
``requires_ffmpeg`` integration test.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from course_supporter.config import settings
from course_supporter.ingestion.base import UnsupportedFormatError
from course_supporter.ingestion.video_pipeline.media import (
    _AUDIO_EXTRACT_TIMEOUT_CAP_SEC,
    _AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC,
    _DOWNLOAD_TIMEOUT_FLOOR_SEC,
    _DOWNLOAD_TIMEOUT_SEC,
    _FRAME_EXTRACT_TIMEOUT_CAP_SEC,
    _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC,
    _parse_probe,
    audio_extract_timeout_for,
    frame_extract_timeout_for,
    probe_intake_duration_from_bytes,
    probe_intake_duration_sec,
)

_RUN_TARGET = "course_supporter.ingestion.video_pipeline.media._run"

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


class TestAudioExtractTimeout:
    """Duration-proportional audio-extraction timeout (task M1+M2, Krok 2)."""

    def test_short_clip_clamps_to_floor(self) -> None:
        # 1-min clip: budget 0.2 → 12 s < FLOOR → fail-fast preserved.
        assert audio_extract_timeout_for(60.0) == _AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC

    def test_mid_length_scales_proportionally(self) -> None:
        # 54-min (3240 s) x 0.2 = 648 s -> between FLOOR and CAP.
        result = audio_extract_timeout_for(3240.0)
        assert result == pytest.approx(648.0)
        assert result > _AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC
        assert result < _AUDIO_EXTRACT_TIMEOUT_CAP_SEC

    def test_contract_150min_clamps_to_cap(self) -> None:
        # 150-min worst case (9000 s) x 0.2 = 1800 s == CAP (the 150-min budget).
        assert audio_extract_timeout_for(9000.0) == _AUDIO_EXTRACT_TIMEOUT_CAP_SEC

    def test_pathological_long_clamps_to_cap(self) -> None:
        # Out-of-contract duration is clamped, never unbounded.
        assert audio_extract_timeout_for(36000.0) == _AUDIO_EXTRACT_TIMEOUT_CAP_SEC

    def test_cap_stays_under_job_timeout(self) -> None:
        # Invariant: audio-extract CAP must fit well inside the ARQ job_timeout.
        assert settings.worker_job_timeout > _AUDIO_EXTRACT_TIMEOUT_CAP_SEC


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


def _run_returning(rc: int, out: bytes, err: bytes = b"") -> AsyncMock:
    """An AsyncMock standing in for media._run -> (returncode, stdout, stderr)."""
    return AsyncMock(return_value=(rc, out, err))


class TestProbeIntakeDurationUrl:
    """yt-dlp metadata-only intake probe (DD-3.3c-I-B URL path)."""

    async def test_reads_duration_field(self) -> None:
        payload = json.dumps({"duration": 9000, "title": "lecture"}).encode()
        with patch(_RUN_TARGET, _run_returning(0, payload)):
            assert await probe_intake_duration_sec("https://youtu.be/x") == 9000.0

    async def test_nonzero_exit_raises(self) -> None:
        """Private/unavailable URL (yt-dlp non-zero exit) → clear error."""
        with (
            patch(_RUN_TARGET, _run_returning(1, b"", b"Private video")),
            pytest.raises(UnsupportedFormatError, match="metadata probe failed"),
        ):
            await probe_intake_duration_sec("https://youtu.be/private")

    async def test_invalid_json_raises(self) -> None:
        with (
            patch(_RUN_TARGET, _run_returning(0, b"not json")),
            pytest.raises(UnsupportedFormatError, match="invalid metadata JSON"),
        ):
            await probe_intake_duration_sec("https://youtu.be/x")

    async def test_missing_duration_raises(self) -> None:
        """A response without a numeric duration (e.g. live) → clear error."""
        payload = json.dumps({"title": "live", "duration": None}).encode()
        with (
            patch(_RUN_TARGET, _run_returning(0, payload)),
            pytest.raises(UnsupportedFormatError, match="could not determine"),
        ):
            await probe_intake_duration_sec("https://youtu.be/live")


class TestProbeIntakeDurationBytes:
    """ffprobe-on-bytes intake probe (DD-3.3c-I-B upload/presigned path)."""

    async def test_returns_duration_seconds(self) -> None:
        payload = json.dumps(_PROBE_OK).encode()
        with patch(_RUN_TARGET, _run_returning(0, payload)):
            # _PROBE_OK duration is 61.5 s.
            result = await probe_intake_duration_from_bytes(b"fake", filename="a.mp4")
        assert result == pytest.approx(61.5)

    async def test_corrupt_container_raises(self) -> None:
        payload = json.dumps({"streams": [], "format": {}}).encode()
        with (
            patch(_RUN_TARGET, _run_returning(0, payload)),
            pytest.raises(UnsupportedFormatError, match="No video stream"),
        ):
            await probe_intake_duration_from_bytes(b"fake", filename="a.mp4")
