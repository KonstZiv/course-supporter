"""Tests for VD pipeline Pydantic schemas."""

import pytest

from course_supporter.vd.schemas import (
    AlignedSegment,
    AlignmentReport,
    ChangeClass,
    CoverageGap,
    EyesResult,
    FrameSamplingResult,
    FrameSource,
    InstantMemory,
    PiPMask,
    SampledFrame,
    SamplingParams,
    Scene,
    SceneAnalysis,
    SceneMemory,
    VDResult,
    VideoMemory,
)

# ---------------------------------------------------------------------------
# Stage A: Frame Sampling
# ---------------------------------------------------------------------------


class TestSampledFrame:
    """Test SampledFrame model."""

    def test_basic_creation(self) -> None:
        frame = SampledFrame(
            frame_id="golden_006_92s",
            filename="golden_006_92s.jpg",
            timestamp_sec=92.0,
            scene_id=3,
            source=FrameSource.GOLDEN,
            dhash="a" * 64,
            dhash_dist=0.25,
            time_gap=10.0,
            change_class=ChangeClass.BOUNDARY,
        )
        assert frame.frame_id == "golden_006_92s"
        assert frame.is_gap_fill is False

    def test_gap_fill_property(self) -> None:
        frame = SampledFrame(
            frame_id="fill_001_47s",
            filename="fill_001_47s.jpg",
            timestamp_sec=47.0,
            scene_id=1,
            source=FrameSource.GAP_FILL,
            dhash="b" * 64,
            dhash_dist=0.05,
            time_gap=18.0,
            change_class=ChangeClass.BOUNDARY,
        )
        assert frame.is_gap_fill is True

    def test_dhash_dist_bounds(self) -> None:
        with pytest.raises(ValueError):
            SampledFrame(
                frame_id="x",
                filename="x.jpg",
                timestamp_sec=0.0,
                scene_id=0,
                source=FrameSource.GOLDEN,
                dhash="aa",
                dhash_dist=1.5,
                time_gap=0.0,
                change_class=ChangeClass.FIRST,
            )

    def test_serialization_roundtrip(self) -> None:
        frame = SampledFrame(
            frame_id="frame_000_0s",
            filename="frame_000_0s.jpg",
            timestamp_sec=0.0,
            scene_id=0,
            source=FrameSource.GOLDEN,
            dhash="abc123",
            dhash_dist=0.0,
            time_gap=0.0,
            change_class=ChangeClass.FIRST,
        )
        data = frame.model_dump()
        restored = SampledFrame.model_validate(data)
        assert restored == frame


class TestScene:
    """Test Scene model."""

    def test_duration(self) -> None:
        scene = Scene(
            scene_id=0,
            frame_ids=["f1", "f2", "f3"],
            start_sec=10.0,
            end_sec=25.0,
        )
        assert scene.duration_sec == pytest.approx(15.0)

    def test_zero_duration(self) -> None:
        scene = Scene(
            scene_id=0,
            frame_ids=["f1"],
            start_sec=5.0,
            end_sec=5.0,
        )
        assert scene.duration_sec == 0.0


class TestPiPMask:
    """Test PiPMask model."""

    def test_valid(self) -> None:
        mask = PiPMask(x=0, y=0, width=320, height=240, confidence=0.73)
        assert mask.width == 320

    def test_zero_width_rejected(self) -> None:
        with pytest.raises(ValueError):
            PiPMask(x=0, y=0, width=0, height=240, confidence=0.5)


class TestSamplingParams:
    """Test SamplingParams defaults."""

    def test_defaults(self) -> None:
        p = SamplingParams()
        assert p.fps == 0.5
        assert p.hash_size == 16
        assert p.gap_fill_max_sec == 15.0
        assert p.scene_boundary_dhash == 0.20
        assert p.scene_boundary_time_gap == 10.0
        assert p.tier1_dhash == 0.15
        assert p.min_votes == 2


class TestFrameSamplingResult:
    """Test FrameSamplingResult model."""

    def test_empty(self) -> None:
        result = FrameSamplingResult(
            frames=[],
            scenes=[],
        )
        assert result.complete is False
        assert result.pip_mask is None
        assert result.video_resolution is None


# ---------------------------------------------------------------------------
# Stage B: Visual Analysis
# ---------------------------------------------------------------------------


class TestEyesResult:
    """Test EyesResult model."""

    def test_creation(self) -> None:
        r = EyesResult(
            frame_id="frame_001_2s",
            timestamp_sec=2.0,
            scene_id=0,
            response="## Scene Composition\n...",
            n_images=2,
            latency_sec=5.6,
            input_tokens=1532,
            output_tokens=837,
        )
        assert r.importance == 3  # default
        assert r.n_images == 2

    def test_n_images_bounds(self) -> None:
        with pytest.raises(ValueError):
            EyesResult(
                frame_id="x",
                timestamp_sec=0.0,
                scene_id=0,
                response="",
                n_images=4,
                latency_sec=0.0,
                input_tokens=0,
                output_tokens=0,
            )


class TestInstantMemory:
    """Test InstantMemory model."""

    def test_basic_creation(self) -> None:
        im = InstantMemory(
            frame_id="f0",
            scene_id=0,
            timestamp_sec=10.0,
            current="Code editor with Python",
        )
        assert im.previous == ""
        assert im.is_delta is False

    def test_with_previous(self) -> None:
        im = InstantMemory(
            frame_id="f1",
            scene_id=0,
            timestamp_sec=12.0,
            current="New function added",
            previous="Code editor with Python",
            is_delta=True,
        )
        assert im.previous == "Code editor with Python"
        assert im.is_delta is True


class TestSceneMemory:
    """Test SceneMemory model."""

    def test_importance_range(self) -> None:
        sm = SceneMemory(
            scene_id=0,
            scene_type="screen_recording",
            summary="Test summary",
            importance=5,
        )
        assert sm.importance == 5

        with pytest.raises(ValueError):
            SceneMemory(
                scene_id=0,
                scene_type="x",
                summary="x",
                importance=6,
            )

    def test_defaults(self) -> None:
        sm = SceneMemory(scene_id=0)
        assert sm.frames_seen == 0
        assert sm.previous_scene_summary == ""
        assert sm.topics == []


class TestVideoMemory:
    """Test VideoMemory model."""

    def test_defaults(self) -> None:
        vm = VideoMemory()
        assert vm.text == ""
        assert vm.scenes_processed == 0


class TestVDResult:
    """Test VDResult model."""

    def test_full_construction(self) -> None:
        scene = Scene(
            scene_id=0,
            frame_ids=["f1"],
            start_sec=0.0,
            end_sec=10.0,
        )
        eyes = EyesResult(
            frame_id="f1",
            timestamp_sec=0.0,
            scene_id=0,
            response="test",
            n_images=1,
            latency_sec=1.0,
            input_tokens=100,
            output_tokens=50,
        )
        scene_mem = SceneMemory(
            scene_id=0,
            scene_type="screen_recording",
            summary="Demo scene",
        )
        analysis = SceneAnalysis(
            scene=scene,
            eyes_results=[eyes],
            scene_memory=scene_mem,
        )
        result = VDResult(
            scenes=[analysis],
            video_memory=VideoMemory(text="ctx", scenes_processed=1),
            frames_total=1,
            frames_analyzed=1,
            model="gemini-3.1-flash-lite-preview",
        )
        assert result.frames_total == 1
        assert len(result.scenes) == 1

    def test_serialization_roundtrip(self) -> None:
        result = VDResult(
            scenes=[],
            video_memory=VideoMemory(),
            frames_total=0,
            frames_analyzed=0,
            model="test-model",
        )
        data = result.model_dump()
        restored = VDResult.model_validate(data)
        assert restored.model == "test-model"


# ---------------------------------------------------------------------------
# Cross-modal Alignment
# ---------------------------------------------------------------------------


class TestAlignmentSchemas:
    """Test alignment schemas."""

    def test_aligned_segment(self) -> None:
        seg = AlignedSegment(
            start_sec=10.0,
            end_sec=20.0,
            stt_text="hello",
            vd_scene_id=3,
            alignment_confidence=0.85,
        )
        assert seg.semantic_overlap == 0.0  # default
        assert seg.conflicts == []

    def test_coverage_gap(self) -> None:
        gap = CoverageGap(
            start_sec=30.0,
            end_sec=35.0,
            gap_type="vd_only",
        )
        assert gap.gap_type == "vd_only"

    def test_alignment_report(self) -> None:
        report = AlignmentReport()
        assert report.segments == []
        assert report.semantic_coverage == 0.0
