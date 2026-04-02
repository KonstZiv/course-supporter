"""Tests for VD frame sampler (pure logic, no FFmpeg/video needed)."""

from __future__ import annotations

from unittest.mock import MagicMock

from course_supporter.vd.frame_sampler import FrameSampler
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
) -> dict[str, object]:
    """Create a mock frame entry for testing pure dedup/segmentation."""
    # Create a mock ImageHash that returns dhash_val on subtraction
    mock_hash = MagicMock()
    mock_hash.__sub__ = MagicMock(return_value=dhash_val)
    mock_hash.__str__ = MagicMock(return_value=f"hash_{ts}")
    return {
        "path": MagicMock(name=f"frame_{ts}s.jpg"),
        "timestamp": ts,
        "dhash": mock_hash,
        "is_fill": is_fill,
    }


class TestDedupWithCooldown:
    """Test dHash dedup with cooldown logic."""

    def test_empty_input(self) -> None:
        result = FrameSampler._dedup_with_cooldown([], SamplingParams())
        assert result == []

    def test_single_frame(self) -> None:
        entries = [_make_entry(0.0)]
        result = FrameSampler._dedup_with_cooldown(entries, SamplingParams())
        assert len(result) == 1

    def test_keeps_different_frames_before_coding_mode(self) -> None:
        """Unstable frames below cooldown_count are kept."""
        p = SamplingParams(
            hash_size=16,
            dhash_threshold=0.05,
            pip_cooldown_count=3,
        )
        threshold = int(16 * 16 * 0.05)  # = 12

        # 2 consecutive unstable frames (< cooldown_count=3) → kept
        entries = [_make_entry(0.0)]
        for i in range(1, 3):
            e = _make_entry(float(i * 2))
            e["dhash"].__sub__ = MagicMock(return_value=threshold + 5)
            entries.append(e)

        result = FrameSampler._dedup_with_cooldown(entries, p)
        # First + 2 unstable = 3 (coding mode not reached)
        assert len(result) == 3

    def test_removes_similar_frames(self) -> None:
        """Frames with distance <= threshold should be removed."""
        p = SamplingParams(hash_size=16, dhash_threshold=0.05)
        threshold = int(16 * 16 * 0.05)

        entries = [_make_entry(0.0)]
        for i in range(1, 5):
            e = _make_entry(float(i * 2))
            e["dhash"].__sub__ = MagicMock(return_value=threshold - 1)
            entries.append(e)

        result = FrameSampler._dedup_with_cooldown(entries, p)
        assert len(result) == 1  # only first frame kept

    def test_coding_mode_activation(self) -> None:
        """3+ consecutive unstable frames trigger coding mode."""
        p = SamplingParams(
            hash_size=16,
            dhash_threshold=0.05,
            pip_cooldown_count=3,
            pip_cooldown_sec=4.0,
        )
        threshold = int(16 * 16 * 0.05)

        # Frame 0 (0s): kept (first)
        # Frame 1 (1s): unstable, consecutive=1 (<3) → kept
        # Frame 2 (2s): unstable, consecutive=2 (<3) → kept
        # Frame 3 (3s): unstable, consecutive=3 → coding_mode! → skipped
        # Frame 4 (4s): unstable, coding_mode → skipped
        # Frame 5 (5s): stable, coding_mode, 5-2=3s < 4s cooldown → skipped
        # Frame 6 (9s): stable, coding_mode, 9-2=7s >= 4s cooldown → kept
        entries = [_make_entry(0.0)]
        for i in range(1, 5):
            e = _make_entry(float(i))
            e["dhash"].__sub__ = MagicMock(return_value=threshold + 10)
            entries.append(e)

        stable1 = _make_entry(5.0)
        stable1["dhash"].__sub__ = MagicMock(return_value=1)
        entries.append(stable1)

        stable2 = _make_entry(9.0)
        stable2["dhash"].__sub__ = MagicMock(return_value=1)
        entries.append(stable2)

        result = FrameSampler._dedup_with_cooldown(entries, p)
        # Frame 0 + Frame 1 + Frame 2 + Frame 6 = 4
        assert len(result) == 4


class TestSegmentScenes:
    """Test scene segmentation logic."""

    def test_empty(self) -> None:
        frames, scenes = FrameSampler._segment_scenes([], SamplingParams())
        assert frames == []
        assert scenes == []

    def test_single_scene(self) -> None:
        """All similar frames → one scene."""
        p = SamplingParams(hash_size=16)

        entries = []
        for i in range(5):
            e = _make_entry(float(i * 2))
            e["dhash"].__sub__ = MagicMock(return_value=5)  # low
            entries.append(e)

        frames, scenes = FrameSampler._segment_scenes(entries, p)
        assert len(scenes) == 1
        assert len(frames) == 5
        assert all(f.scene_id == 0 for f in frames)
        assert frames[0].change_class == ChangeClass.FIRST

    def test_multiple_scenes(self) -> None:
        """High-distance frames create scene boundaries."""
        p = SamplingParams(hash_size=16)
        max_bits = 16 * 16

        entries = []
        for i in range(6):
            e = _make_entry(float(i * 2))
            # Frame 3 creates a scene boundary (high distance)
            if i == 3:
                e["dhash"].__sub__ = MagicMock(
                    return_value=int(max_bits * 0.25),
                )
            else:
                e["dhash"].__sub__ = MagicMock(return_value=2)
            entries.append(e)

        frames, scenes = FrameSampler._segment_scenes(entries, p)
        assert len(scenes) == 2
        assert scenes[0].scene_id == 0
        assert scenes[1].scene_id == 1
        assert frames[3].change_class == ChangeClass.BOUNDARY

    def test_time_gap_boundary(self) -> None:
        """Time gap >10s creates a scene boundary."""
        p = SamplingParams(
            hash_size=16,
            scene_boundary_time_gap=10.0,
        )

        entries = [
            _make_entry(0.0),
            _make_entry(2.0),
            _make_entry(15.0),  # 13s gap → boundary
            _make_entry(17.0),
        ]
        for e in entries:
            e["dhash"].__sub__ = MagicMock(return_value=2)  # low visual

        frames, scenes = FrameSampler._segment_scenes(entries, p)
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

        frames, _ = FrameSampler._segment_scenes(entries, p)
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

        frames, scenes = FrameSampler._segment_scenes(entries, p)
        assert len(scenes) == 1
        assert len(scenes[0].frame_ids) == 4
        assert scenes[0].frame_ids == [f.frame_id for f in frames]

    def test_medium_change_class(self) -> None:
        """Distance between 10% and 20% → MEDIUM."""
        p = SamplingParams(hash_size=16)
        max_bits = 16 * 16

        entries = [
            _make_entry(0.0),
            _make_entry(2.0),
        ]
        # 15% distance → MEDIUM
        entries[1]["dhash"].__sub__ = MagicMock(
            return_value=int(max_bits * 0.15),
        )

        frames, _ = FrameSampler._segment_scenes(entries, p)
        assert frames[1].change_class == ChangeClass.MEDIUM
