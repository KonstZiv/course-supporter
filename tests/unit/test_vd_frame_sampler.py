"""Tests for VD frame sampler."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.vd.frame_sampler import (
    FrameSampler,
    _build_zones,
    _ffmpeg_extract_fps,
    _ffmpeg_extract_single,
    _FrameEntry,
    _FrameMetrics,
    _get_video_resolution,
)
from course_supporter.vd.schemas import (
    ChangeClass,
    FrameSource,
    SamplingParams,
)


def _make_entry(
    ts: float,
    dhash_val: int = 0,
    *,
    is_fill: bool = False,
) -> _FrameEntry:
    """Create a mock frame entry for testing pure dedup/segmentation."""
    mock_hash = MagicMock()
    mock_hash.__sub__ = MagicMock(return_value=dhash_val)
    mock_hash.__str__ = MagicMock(return_value=f"hash_{ts}")
    return _FrameEntry(
        path=MagicMock(name=f"frame_{ts}s.jpg"),
        timestamp=ts,
        dhash=mock_hash,
        is_fill=is_fill,
    )


def _patch_compare(metrics: _FrameMetrics):
    """Patch _compare_frames to return fixed metrics."""
    return patch(
        "course_supporter.vd.frame_sampler._compare_frames",
        return_value=metrics,
    )


# Reusable metric sets
_IDENTICAL = _FrameMetrics(0.0, 0.0, 0.0, 1.0, 0.0)
_TIER1_DHASH = _FrameMetrics(0.20, 0.05, 0.10, 0.90, 0.08)
_TIER1_PIXEL = _FrameMetrics(0.02, 0.15, 0.05, 0.96, 0.03)
_TWO_VOTES = _FrameMetrics(0.02, 0.03, 0.20, 0.96, 0.02)  # pixel + color
_ONE_VOTE = _FrameMetrics(0.01, 0.03, 0.05, 0.98, 0.01)  # only pixel
_THREE_VOTES = _FrameMetrics(0.04, 0.03, 0.20, 0.93, 0.02)  # dhash+pixel+color+ssim


class TestDedupVoting:
    """Test multi-metric dedup with tiered voting."""

    def test_empty_input(self) -> None:
        result = FrameSampler._dedup_voting([], SamplingParams(), None)
        assert result == []

    def test_single_frame(self) -> None:
        entries = [_make_entry(0.0)]
        result = FrameSampler._dedup_voting(entries, SamplingParams(), None)
        assert len(result) == 1

    def test_tier1_dhash_keeps(self) -> None:
        """Strong dHash signal (>15%) keeps frame unconditionally."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_TIER1_DHASH):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(),
                None,
            )
        assert len(result) == 2

    def test_tier1_pixel_diff_keeps(self) -> None:
        """Strong pixel_diff signal (>10%) keeps frame unconditionally."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_TIER1_PIXEL):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(),
                None,
            )
        assert len(result) == 2

    def test_identical_frames_skipped(self) -> None:
        """Identical frames get 0 votes and are skipped."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_IDENTICAL):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(),
                None,
            )
        assert len(result) == 1

    def test_two_votes_keeps(self) -> None:
        """2 out of 5 votes (min_votes=2) keeps frame."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_TWO_VOTES):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(min_votes=2),
                None,
            )
        assert len(result) == 2

    def test_one_vote_skips(self) -> None:
        """1 out of 5 votes (min_votes=2) skips frame."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_ONE_VOTE):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(min_votes=2),
                None,
            )
        assert len(result) == 1

    def test_min_votes_configurable(self) -> None:
        """Raising min_votes to 3 makes 2-vote frames skip."""
        entries = [_make_entry(0.0), _make_entry(2.0)]
        with _patch_compare(_TWO_VOTES):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(min_votes=3),
                None,
            )
        assert len(result) == 1

    def test_compares_against_last_kept(self) -> None:
        """Each frame is compared to the last KEPT frame, not previous."""
        call_args: list[tuple] = []
        original_metrics = [_IDENTICAL, _TWO_VOTES]
        call_count = 0

        def mock_compare(*args, **kwargs):
            nonlocal call_count
            m = original_metrics[min(call_count, len(original_metrics) - 1)]
            call_count += 1
            call_args.append(args)
            return m

        entries = [_make_entry(0.0), _make_entry(2.0), _make_entry(4.0)]
        with patch(
            "course_supporter.vd.frame_sampler._compare_frames",
            side_effect=mock_compare,
        ):
            result = FrameSampler._dedup_voting(
                entries,
                SamplingParams(),
                None,
            )

        # Frame 1: identical to frame 0 → skipped
        # Frame 2: compared to frame 0 (last kept), 2 votes → kept
        assert len(result) == 2
        # Second comparison should use frame 0's path (last kept)
        assert call_args[1][0] == entries[0]["path"]


class TestSegmentScenes:
    """Test scene segmentation logic."""

    def test_empty(self) -> None:
        frames, scenes = FrameSampler._segment_scenes([], SamplingParams(), None)
        assert frames == []
        assert scenes == []

    def test_single_scene(self) -> None:
        """All similar frames -> one scene."""
        p = SamplingParams(hash_size=16)

        entries = []
        for i in range(5):
            e = _make_entry(float(i * 2))
            e["dhash"].__sub__ = MagicMock(return_value=5)
            entries.append(e)

        frames, scenes = FrameSampler._segment_scenes(entries, p, None)
        assert len(scenes) == 1
        assert len(frames) == 5
        assert all(f.scene_id == 0 for f in frames)
        assert frames[0].change_class == ChangeClass.FIRST

    def test_multiple_scenes_dhash_plus_color(self) -> None:
        """dHash + colour confirm creates a boundary."""
        p = SamplingParams(hash_size=16)
        max_bits = 16 * 16

        entries = []
        for i in range(6):
            e = _make_entry(float(i * 2))
            if i == 3:
                e["dhash"].__sub__ = MagicMock(
                    return_value=int(max_bits * 0.25),
                )
            else:
                e["dhash"].__sub__ = MagicMock(return_value=2)
            entries.append(e)

        # Color histogram confirms + flow says no coherent motion
        with (
            patch(
                "course_supporter.vd.frame_sampler._color_hist_distance",
                return_value=0.5,
            ),
            patch(
                "course_supporter.vd.frame_sampler._flow_coherence",
                return_value=0.1,
            ),
        ):
            frames, scenes = FrameSampler._segment_scenes(
                entries,
                p,
                None,
            )
        assert len(scenes) == 2
        assert frames[3].change_class == ChangeClass.BOUNDARY

    def test_motion_not_boundary(self) -> None:
        """High dHash but same colours = motion, not a new scene."""
        p = SamplingParams(hash_size=16)
        max_bits = 16 * 16

        entries = []
        for i in range(4):
            e = _make_entry(float(i * 2))
            # Every frame has high dHash (e.g. rotating object)
            e["dhash"].__sub__ = MagicMock(
                return_value=int(max_bits * 0.30),
            )
            entries.append(e)

        # Color histogram says: same colours → motion
        with patch(
            "course_supporter.vd.frame_sampler._color_hist_distance",
            return_value=0.05,
        ):
            frames, scenes = FrameSampler._segment_scenes(
                entries,
                p,
                None,
            )
        # All frames in one scene (motion, not real boundaries)
        assert len(scenes) == 1
        assert all(f.change_class != ChangeClass.BOUNDARY for f in frames[1:])

    def test_time_gap_boundary(self) -> None:
        """Time gap >10s creates a scene boundary."""
        p = SamplingParams(hash_size=16, scene_boundary_time_gap=10.0)

        entries = [
            _make_entry(0.0),
            _make_entry(2.0),
            _make_entry(15.0),  # 13s gap
            _make_entry(17.0),
        ]
        for e in entries:
            e["dhash"].__sub__ = MagicMock(return_value=2)

        frames, scenes = FrameSampler._segment_scenes(entries, p, None)
        assert len(scenes) == 2
        assert frames[2].change_class == ChangeClass.BOUNDARY

    def test_gap_fill_source(self) -> None:
        """Gap fill frames get FrameSource.GAP_FILL."""
        p = SamplingParams(hash_size=16)
        entries = [
            _make_entry(0.0),
            _make_entry(10.0, is_fill=True),
        ]
        for e in entries:
            e["dhash"].__sub__ = MagicMock(return_value=2)

        frames, _ = FrameSampler._segment_scenes(entries, p, None)
        assert frames[0].source == FrameSource.GOLDEN
        assert frames[1].source == FrameSource.GAP_FILL

    def test_scene_frame_ids_match(self) -> None:
        """Scene.frame_ids must contain all frames in that scene."""
        p = SamplingParams(hash_size=16)
        entries = []
        for i in range(4):
            e = _make_entry(float(i * 2))
            e["dhash"].__sub__ = MagicMock(return_value=2)
            entries.append(e)

        frames, scenes = FrameSampler._segment_scenes(entries, p, None)
        assert len(scenes) == 1
        assert len(scenes[0].frame_ids) == 4
        assert scenes[0].frame_ids == [f.frame_id for f in frames]

    def test_medium_change_class(self) -> None:
        """Distance between 10% and 20% -> MEDIUM."""
        p = SamplingParams(hash_size=16)
        max_bits = 16 * 16

        entries = [
            _make_entry(0.0),
            _make_entry(2.0),
        ]
        entries[1]["dhash"].__sub__ = MagicMock(
            return_value=int(max_bits * 0.15),
        )

        frames, _ = FrameSampler._segment_scenes(entries, p, None)
        assert frames[1].change_class == ChangeClass.MEDIUM


# -- _build_zones ----------------------------------------------------------


class TestBuildZones:
    def test_eight_zones(self) -> None:
        zones = _build_zones(1280, 720)
        assert len(zones) == 8

    def test_zone_names(self) -> None:
        zones = _build_zones(1280, 720)
        expected = {
            "top_left",
            "top_right",
            "bottom_left",
            "bottom_right",
            "top_center",
            "bottom_center",
            "left_center",
            "right_center",
        }
        assert set(zones.keys()) == expected

    def test_zones_within_bounds(self) -> None:
        w, h = 1280, 720
        zones = _build_zones(w, h)
        for rect in zones.values():
            assert 0 <= rect.x1 < rect.x2 <= w
            assert 0 <= rect.y1 < rect.y2 <= h


# -- _ffmpeg_extract_fps (mocked subprocess) --------------------------------


class TestFfmpegExtractFps:
    async def test_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d) / "frames"
            out_dir.mkdir()
            # Create fake frame files that ffmpeg would produce
            for i in range(3):
                (out_dir / f"frame_{i + 1:06d}.jpg").write_bytes(b"img")

            mock_proc = AsyncMock(returncode=0)
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = await _ffmpeg_extract_fps(
                    Path("/fake/video.mp4"), 1.0, out_dir
                )

            assert len(result) == 3

    async def test_ffmpeg_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mock_proc = AsyncMock(returncode=1)
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Error"))

            with (
                patch(
                    "asyncio.create_subprocess_exec",
                    return_value=mock_proc,
                ),
                pytest.raises(RuntimeError, match="FFmpeg fps extraction"),
            ):
                await _ffmpeg_extract_fps(Path("/fake/video.mp4"), 1.0, Path(d))


# -- _ffmpeg_extract_single (mocked subprocess) ----------------------------


class TestFfmpegExtractSingle:
    async def test_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "frame.jpg"

            mock_proc = AsyncMock(returncode=0)
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.kill = MagicMock()

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                output.write_bytes(b"img")  # simulate ffmpeg creating file
                result = await _ffmpeg_extract_single(
                    Path("/fake/video.mp4"), 10.0, output
                )

            assert result is True

    async def test_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "frame.jpg"

            mock_proc = AsyncMock(returncode=1)
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.kill = MagicMock()

            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = await _ffmpeg_extract_single(
                    Path("/fake/video.mp4"), 10.0, output
                )

            assert result is False


# -- _get_video_resolution (mocked ffprobe) --------------------------------


class TestGetVideoResolution:
    async def test_parses_resolution(self) -> None:
        mock_proc = AsyncMock(returncode=0)
        mock_proc.communicate = AsyncMock(return_value=(b"1280x720\n", b""))
        mock_proc.kill = MagicMock()

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            w, h = await _get_video_resolution(Path("/fake/video.mp4"))

        assert w == 1280
        assert h == 720

    async def test_invalid_output_raises(self) -> None:
        mock_proc = AsyncMock(returncode=0)
        mock_proc.communicate = AsyncMock(return_value=(b"invalid\n", b""))
        mock_proc.kill = MagicMock()

        with (
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            pytest.raises(RuntimeError, match="Cannot parse"),
        ):
            await _get_video_resolution(Path("/fake/video.mp4"))


# -- FrameSampler.sample (integration, fully mocked) -----------------------


class TestFrameSamplerSample:
    async def test_sample_orchestration(self) -> None:
        """Full sample() with all I/O mocked."""
        with tempfile.TemporaryDirectory() as d:
            frame_dir = Path(d)
            # Create fake frame files
            for i in range(5):
                (frame_dir / f"frame_{i + 1:06d}.jpg").write_bytes(b"fake_img")

            sampler = FrameSampler(SamplingParams(fps=1.0))

            mock_hash = MagicMock()
            mock_hash.__sub__ = MagicMock(return_value=0)
            mock_hash.__str__ = MagicMock(return_value="abcd1234")

            with (
                patch(
                    "course_supporter.vd.frame_sampler._get_video_resolution",
                    new=AsyncMock(return_value=(1280, 720)),
                ),
                patch(
                    "course_supporter.vd.frame_sampler._ffmpeg_extract_fps",
                    new=AsyncMock(
                        return_value=[
                            frame_dir / f"frame_{i + 1:06d}.jpg" for i in range(5)
                        ]
                    ),
                ),
                patch(
                    "course_supporter.vd.frame_sampler._detect_pip",
                    return_value=None,
                ),
                patch(
                    "course_supporter.vd.frame_sampler._compute_dhash",
                    return_value=mock_hash,
                ),
                patch(
                    "course_supporter.vd.frame_sampler._compare_frames",
                    return_value=_FrameMetrics(
                        dhash_dist=0.01,
                        pixel_diff=0.01,
                        color_hist=0.1,
                        ssim=0.99,
                        edge_diff=0.01,
                    ),
                ),
            ):
                result = await sampler.sample(Path("/fake/video.mp4"), frame_dir)

            assert result.frames is not None
            assert result.scenes is not None
            assert len(result.scenes) >= 1
