"""Tests for VD frame sampler (pure logic, no FFmpeg/video needed)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from course_supporter.vd.frame_sampler import (
    FrameSampler,
    _FrameEntry,
    _FrameMetrics,
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
