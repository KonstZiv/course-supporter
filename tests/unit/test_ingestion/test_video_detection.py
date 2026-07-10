"""Tests for Krok 3 detection (Phase 2.4 task 2.4.3).

The cv2 algorithm (dedup voting, scene segmentation, PiP, metrics) is
exercised deterministically on synthetic JPEGs written with cv2 — no
ffmpeg, no network. One ``requires_ffmpeg`` test runs the full async
``detect`` over a runtime-generated synthetic multi-scene video,
validating the ffmpeg→detect chain + the spike preservation gate (a
high-edge "code-like" frame survives dedup).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from course_supporter.ingestion.video_pipeline import detection
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    DetectionResult,
    VideoFileMetadata,
)

_PARAMS = detection.SamplingParams()


def _write_solid(path: Path, bgr: tuple[int, int, int]) -> Path:
    cv2.imwrite(str(path), np.full((240, 320, 3), bgr, dtype=np.uint8))
    return path


def _write_noise(path: Path, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    cv2.imwrite(str(path), rng.integers(0, 256, (240, 320, 3), dtype=np.uint8))
    return path


def _entry(path: Path, ts: float) -> detection._Entry:
    gray = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2GRAY)
    return detection._Entry(
        path=path,
        timestamp_sec=ts,
        dhash=detection._compute_dhash(gray, _PARAMS.hash_size),
    )


class TestDedupVoting:
    def test_drops_identical_frame(self, tmp_path: Path) -> None:
        """An identical consecutive frame is deduplicated away."""
        a = _write_solid(tmp_path / "a.jpg", (200, 120, 40))
        b = _write_solid(tmp_path / "b.jpg", (200, 120, 40))  # same colour
        kept = detection._dedup_voting([_entry(a, 0.0), _entry(b, 2.0)], _PARAMS, None)
        assert len(kept) == 1

    def test_keeps_distinct_frame(self, tmp_path: Path) -> None:
        """A visually distinct frame is kept (Tier 1 pixel_diff)."""
        a = _write_solid(tmp_path / "a.jpg", (255, 0, 0))  # blue
        b = _write_solid(tmp_path / "b.jpg", (0, 0, 255))  # red
        kept = detection._dedup_voting([_entry(a, 0.0), _entry(b, 2.0)], _PARAMS, None)
        assert len(kept) == 2

    def test_first_frame_always_kept(self, tmp_path: Path) -> None:
        a = _write_solid(tmp_path / "a.jpg", (10, 10, 10))
        kept = detection._dedup_voting([_entry(a, 0.0)], _PARAMS, None)
        assert len(kept) == 1


class TestSceneSegmentation:
    def test_time_gap_forces_boundary(self, tmp_path: Path) -> None:
        """A gap > scene_boundary_time_gap splits scenes even if identical."""
        a = _write_solid(tmp_path / "a.jpg", (40, 40, 40))
        b = _write_solid(tmp_path / "b.jpg", (40, 40, 40))
        scenes = detection._segment_scenes(
            [_entry(a, 0.0), _entry(b, 20.0)], _PARAMS, None
        )
        assert len(scenes) == 2
        assert scenes[0].frames[0].change_class == ChangeClass.FIRST
        assert scenes[1].frames[0].change_class == ChangeClass.BOUNDARY

    def test_similar_frames_single_scene(self, tmp_path: Path) -> None:
        """Near-identical frames close in time stay in one scene."""
        a = _write_solid(tmp_path / "a.jpg", (40, 40, 40))
        b = _write_solid(tmp_path / "b.jpg", (40, 40, 40))
        scenes = detection._segment_scenes(
            [_entry(a, 0.0), _entry(b, 2.0)], _PARAMS, None
        )
        assert len(scenes) == 1
        assert len(scenes[0].frames) == 2

    def test_frame_path_populated(self, tmp_path: Path) -> None:
        a = _write_solid(tmp_path / "a.jpg", (40, 40, 40))
        scenes = detection._segment_scenes([_entry(a, 0.0)], _PARAMS, None)
        assert scenes[0].frames[0].frame_path == a


class TestDetectTimeoutWiring:
    """``detect`` threads a duration-proportional ffmpeg timeout (task 3.3c-B)."""

    async def test_passes_duration_proportional_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The frame-extract timeout is derived from the probed duration.

        Stubs ``extract_frames_fps`` to capture the timeout and abort before
        the cv2 chain — the only behaviour under test is the wiring of
        ``duration_ms / 1000`` through ``frame_extract_timeout_for``.
        """
        from course_supporter.ingestion.video_pipeline.media import (
            frame_extract_timeout_for,
        )

        captured: dict[str, float] = {}

        class _StopExtractError(Exception):
            pass

        async def _fake_extract(
            video_path: Path,
            fps: float,
            dest_dir: Path,
            *,
            timeout_sec: float,
        ) -> list[Path]:
            captured["timeout_sec"] = timeout_sec
            raise _StopExtractError

        monkeypatch.setattr(detection.media, "extract_frames_fps", _fake_extract)

        # 54-min patient-zero video → proportional budget (not the FLOOR).
        meta = VideoFileMetadata(
            duration_ms=3_263_000, codec="h264", resolution="1920x1080"
        )
        with pytest.raises(_StopExtractError):
            await detection.detect(tmp_path / "lesson.mp4", meta, tmp_path)

        assert captured["timeout_sec"] == frame_extract_timeout_for(3263.0)


class TestMetricsAndPip:
    def test_compare_identical_low_change(self, tmp_path: Path) -> None:
        """Identical frames: zero pixel change, near-perfect SSIM."""
        a = _write_noise(tmp_path / "a.jpg", 1)
        cache = detection._DecodeCache(None)
        gray = cache.gray(a)
        assert detection._pixel_diff(gray, gray, _PARAMS) == 0.0
        assert detection._compute_ssim(gray, gray) > 0.99

    def test_compare_distinct_high_change(self, tmp_path: Path) -> None:
        """Opposite-colour frames: large pixel change."""
        a = _write_solid(tmp_path / "a.jpg", (255, 0, 0))
        b = _write_solid(tmp_path / "b.jpg", (0, 0, 255))
        cache = detection._DecodeCache(None)
        assert detection._pixel_diff(cache.gray(a), cache.gray(b), _PARAMS) > 0.5

    def test_pip_none_without_motion(self, tmp_path: Path) -> None:
        """Static identical frames → no PiP corner detected."""
        a = _write_solid(tmp_path / "a.jpg", (40, 40, 40))
        b = _write_solid(tmp_path / "b.jpg", (40, 40, 40))
        assert detection._detect_pip([a, b], 320, 240) is None

    def test_downscale_bounds_longest_side(self) -> None:
        big = np.zeros((1080, 1920), dtype=np.uint8)
        out = detection._downscale(big, 480)
        assert max(out.shape[:2]) == 480
        small = np.zeros((100, 120), dtype=np.uint8)
        assert detection._downscale(small, 480).shape == small.shape


# ── requires_ffmpeg: full detect over a real synthetic video ───────────


@pytest.fixture
def code_slides_video(tmp_path: Path) -> Path:
    """3-scene synthetic video: blue → SMPTE bars (high-edge) → red.

    Generated via ffmpeg (no committed binary). The bars segment is a
    static high-edge "code-like" slide whose preservation through dedup
    is the spike's hard gate.
    """
    out = tmp_path / "slides.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=3:r=2",
            "-f",
            "lavfi",
            "-i",
            "smptebars=s=320x240:d=3:r=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=3:r=2",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.requires_ffmpeg
class TestDetectRealVideo:
    async def test_detects_scenes_and_preserves_high_edge_frame(
        self,
        code_slides_video: Path,
        tmp_path: Path,
    ) -> None:
        """Real ffmpeg extraction + detection: multi-scene + bars kept."""
        meta = VideoFileMetadata(duration_ms=9_000, codec="h264", resolution="320x240")
        work = tmp_path / "work"
        work.mkdir()

        result = await detection.detect(code_slides_video, meta, work)

        assert isinstance(result, DetectionResult)
        # blue / bars / red are visually distinct → more than one scene.
        assert len(result.scenes) >= 2
        all_frames = [f for s in result.scenes for f in s.frames]
        # Every kept frame references an existing raw JPEG.
        assert all(f.frame_path.exists() for f in all_frames)
        # The high-edge bars frame (mid video, ~3-6 s) survived dedup.
        mid = [f for f in all_frames if 3_000 <= f.frame_position_ms <= 6_000]
        assert mid, "high-edge bars frame was dropped by dedup"
