"""Tests for VDPipeline orchestrator — no real API/ffmpeg calls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.vd.pipeline import VDPipeline
from course_supporter.vd.schemas import (
    EyesResult,
    FrameSamplingResult,
    FrameSource,
    SampledFrame,
    Scene,
    SceneMemory,
    VideoMemory,
)


def _make_frame(
    frame_id: str,
    scene_id: int,
    ts: float,
) -> SampledFrame:
    return SampledFrame(
        frame_id=frame_id,
        filename=f"{frame_id}.jpg",
        timestamp_sec=ts,
        scene_id=scene_id,
        source=FrameSource.GOLDEN,
        dhash="a" * 64,
        dhash_dist=0.0,
        time_gap=2.0,
        change_class="first",
    )


def _make_eyes(frame_id: str, scene_id: int, ts: float) -> EyesResult:
    return EyesResult(
        frame_id=frame_id,
        timestamp_sec=ts,
        scene_id=scene_id,
        response="## Scene Composition\n**Setting:** screen_recording",
        n_images=1,
        latency_sec=1.0,
        input_tokens=100,
        output_tokens=50,
        description="Code editor",
    )


def _make_sampling_result(n_scenes: int = 2) -> FrameSamplingResult:
    """Create a FrameSamplingResult with n_scenes, 2 frames each."""
    frames: list[SampledFrame] = []
    scenes: list[Scene] = []
    for s in range(n_scenes):
        f1 = _make_frame(f"f{s}_0", s, float(s * 10))
        f2 = _make_frame(f"f{s}_1", s, float(s * 10 + 2))
        frames.extend([f1, f2])
        scenes.append(
            Scene(
                scene_id=s,
                frame_ids=[f1.frame_id, f2.frame_id],
                start_sec=float(s * 10),
                end_sec=float(s * 10 + 4),
            ),
        )
    return FrameSamplingResult(frames=frames, scenes=scenes, complete=True)


class TestVDPipelineProcess:
    """Test VDPipeline.process orchestration."""

    @pytest.fixture
    def sampling_result(self) -> FrameSamplingResult:
        return _make_sampling_result(n_scenes=2)

    @pytest.fixture
    def mock_sampler(self, sampling_result: FrameSamplingResult) -> MagicMock:
        sampler = MagicMock()
        sampler.sample = AsyncMock(return_value=sampling_result)
        return sampler

    @pytest.fixture
    def mock_analyzer(self) -> MagicMock:
        analyzer = MagicMock()
        analyzer.model = "test-model"

        async def fake_analyze_scene(
            scene: Scene,
            frames: list[SampledFrame],
            _frame_dir: Path,
            **kwargs: object,
        ) -> tuple[list[EyesResult], SceneMemory | None]:
            eyes = [
                _make_eyes(f.frame_id, scene.scene_id, f.timestamp_sec) for f in frames
            ]
            sm = SceneMemory(
                scene_id=scene.scene_id,
                summary=f"Scene {scene.scene_id} summary",
                scene_type="screen_recording",
                importance=3,
                frames_seen=len(frames),
            )
            return eyes, sm

        analyzer.analyze_scene = AsyncMock(side_effect=fake_analyze_scene)
        return analyzer

    @pytest.fixture
    def mock_memory(self) -> MagicMock:
        memory = MagicMock()

        async def fake_update_video(
            scene_memory: SceneMemory,
            video_memory: VideoMemory,
            previous_scene: SceneMemory | None,
        ) -> VideoMemory:
            scenes = video_memory.scenes_processed + 1
            return VideoMemory(
                text=f"Video context after {scenes} scenes",
                scenes_processed=scenes,
            )

        memory.update_video_memory = AsyncMock(side_effect=fake_update_video)
        return memory

    @pytest.mark.asyncio
    async def test_basic_pipeline(
        self,
        mock_sampler: MagicMock,
        mock_analyzer: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        pipeline = VDPipeline(mock_sampler, mock_analyzer, mock_memory)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                result = await pipeline.process(Path("test.mp4"))

        assert result.model == "test-model"
        assert result.frames_total == 4
        assert result.frames_analyzed == 4
        assert len(result.scenes) == 2
        assert result.video_memory.scenes_processed == 2

    @pytest.mark.asyncio
    async def test_scenes_have_correct_data(
        self,
        mock_sampler: MagicMock,
        mock_analyzer: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        pipeline = VDPipeline(mock_sampler, mock_analyzer, mock_memory)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                result = await pipeline.process(Path("test.mp4"))

        s0 = result.scenes[0]
        assert s0.scene.scene_id == 0
        assert len(s0.eyes_results) == 2
        assert s0.scene_memory.scene_type == "screen_recording"
        assert s0.scene_memory.frames_seen == 2

        s1 = result.scenes[1]
        assert s1.scene.scene_id == 1

    @pytest.mark.asyncio
    async def test_video_memory_accumulates(
        self,
        mock_sampler: MagicMock,
        mock_analyzer: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        pipeline = VDPipeline(mock_sampler, mock_analyzer, mock_memory)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                result = await pipeline.process(Path("test.mp4"))

        assert mock_memory.update_video_memory.call_count == 2
        assert result.video_memory.text == "Video context after 2 scenes"

    @pytest.mark.asyncio
    async def test_previous_scene_passed_to_analyzer(
        self,
        mock_sampler: MagicMock,
        mock_analyzer: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        pipeline = VDPipeline(mock_sampler, mock_analyzer, mock_memory)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                await pipeline.process(Path("test.mp4"))

        # Second call should have scene_memory with previous_scene_summary
        second_call = mock_analyzer.analyze_scene.call_args_list[1]
        scene_memory = second_call.kwargs.get("scene_memory")
        assert scene_memory is not None
        assert "Scene 0 summary" in scene_memory.previous_scene_summary

    @pytest.mark.asyncio
    async def test_without_memory_pipeline(
        self,
        mock_sampler: MagicMock,
        mock_analyzer: MagicMock,
    ) -> None:
        """Pipeline works without MemoryPipeline (no video memory updates)."""

        # Analyzer returns None for scene_memory when no memory injected
        async def no_memory_analyze(
            scene: Scene,
            frames: list[SampledFrame],
            _frame_dir: Path,
            **kwargs: object,
        ) -> tuple[list[EyesResult], None]:
            eyes = [
                _make_eyes(f.frame_id, scene.scene_id, f.timestamp_sec) for f in frames
            ]
            return eyes, None

        mock_analyzer.analyze_scene = AsyncMock(side_effect=no_memory_analyze)

        pipeline = VDPipeline(mock_sampler, mock_analyzer, memory=None)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                result = await pipeline.process(Path("test.mp4"))

        assert result.frames_analyzed == 4
        assert result.video_memory.scenes_processed == 0
        assert result.video_memory.text == ""

    @pytest.mark.asyncio
    async def test_empty_scene_skipped(
        self,
        mock_analyzer: MagicMock,
        mock_memory: MagicMock,
    ) -> None:
        """Scenes with no frames are skipped."""
        sampling = FrameSamplingResult(
            frames=[_make_frame("f0", 0, 0.0)],
            scenes=[
                Scene(scene_id=0, frame_ids=["f0"], start_sec=0.0, end_sec=2.0),
                Scene(scene_id=1, frame_ids=["f1"], start_sec=5.0, end_sec=7.0),
            ],
            complete=True,
        )
        sampler = MagicMock()
        sampler.sample = AsyncMock(return_value=sampling)

        pipeline = VDPipeline(sampler, mock_analyzer, mock_memory)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_test"
            with patch("course_supporter.vd.pipeline.shutil"):
                result = await pipeline.process(Path("test.mp4"))

        # Only scene 0 has matching frames
        assert len(result.scenes) == 1
        assert result.scenes[0].scene.scene_id == 0


class TestVDPipelineCleanup:
    """Test temp directory cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_on_success(self) -> None:
        sampler = MagicMock()
        sampler.sample = AsyncMock(
            return_value=_make_sampling_result(n_scenes=0),
        )
        analyzer = MagicMock()
        analyzer.model = "test"

        pipeline = VDPipeline(sampler, analyzer)

        with patch("course_supporter.vd.pipeline.tempfile") as mock_tmp:
            mock_tmp.mkdtemp.return_value = "/tmp/vd_cleanup"
            with patch("course_supporter.vd.pipeline.shutil") as mock_shutil:
                await pipeline.process(Path("test.mp4"))

        mock_shutil.rmtree.assert_called_once_with(
            Path("/tmp/vd_cleanup"),
            ignore_errors=True,
        )

    @pytest.mark.asyncio
    async def test_cleanup_on_error(self) -> None:
        sampler = MagicMock()
        sampler.sample = AsyncMock(side_effect=RuntimeError("ffmpeg failed"))
        analyzer = MagicMock()
        analyzer.model = "test"

        pipeline = VDPipeline(sampler, analyzer)

        with (
            patch("course_supporter.vd.pipeline.tempfile") as mock_tmp,
            patch("course_supporter.vd.pipeline.shutil") as mock_shutil,
        ):
            mock_tmp.mkdtemp.return_value = "/tmp/vd_error"
            with pytest.raises(RuntimeError, match="ffmpeg failed"):
                await pipeline.process(Path("test.mp4"))

        mock_shutil.rmtree.assert_called_once_with(
            Path("/tmp/vd_error"),
            ignore_errors=True,
        )
