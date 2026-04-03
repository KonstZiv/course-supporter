"""VD Pipeline orchestrator: video file → VDResult.

Connects FrameSampler → VisualAnalyzer (with MemoryPipeline) → VDResult.
Self-contained — knows nothing about STT or ingestion.

Usage::

    pipeline = VDPipeline(sampler, analyzer, memory)
    result = await pipeline.process(Path("video.mp4"))
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import structlog

from course_supporter.vd.frame_sampler import FrameSampler
from course_supporter.vd.memory_pipeline import MemoryPipeline
from course_supporter.vd.schemas import (
    SceneAnalysis,
    SceneMemory,
    VDResult,
    VideoMemory,
)
from course_supporter.vd.visual_analyzer import VisualAnalyzer

logger = structlog.get_logger()


class VDPipeline:
    """Orchestrate the full VD pipeline for a single video.

    Args:
        sampler: Frame extraction and scene segmentation.
        analyzer: Per-frame Vision LLM analysis (with optional memory).
        memory: Memory pipeline for video-level context updates.
            Required for video memory updates between scenes.
    """

    def __init__(
        self,
        sampler: FrameSampler,
        analyzer: VisualAnalyzer,
        memory: MemoryPipeline | None = None,
    ) -> None:
        self._sampler = sampler
        self._analyzer = analyzer
        self._memory = memory

    async def process(self, video_path: Path) -> VDResult:
        """Run the full VD pipeline on a video file.

        1. Extract and sample frames (Stage A).
        2. For each scene: analyze frames with streaming memory (Stage B).
        3. After each scene: update video memory.
        4. Return assembled VDResult.

        Temp directory is always cleaned up, even on error.
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="vd_"))
        logger.info("vd_pipeline_start", video=str(video_path), temp_dir=str(temp_dir))

        try:
            return await self._run(video_path, temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("vd_pipeline_cleanup", temp_dir=str(temp_dir))

    async def _run(self, video_path: Path, temp_dir: Path) -> VDResult:
        """Pipeline core — separated from cleanup logic."""
        # Stage A: Frame sampling
        sampling = await self._sampler.sample(video_path, temp_dir)
        logger.info(
            "vd_stage_a_done",
            frames=len(sampling.frames),
            scenes=len(sampling.scenes),
        )

        # Stage B: Visual analysis with streaming memory
        scene_analyses: list[SceneAnalysis] = []
        video_memory = VideoMemory()
        previous_scene: SceneMemory | None = None
        frames_analyzed = 0

        for scene in sampling.scenes:
            scene_frames = [f for f in sampling.frames if f.scene_id == scene.scene_id]
            if not scene_frames:
                continue

            # Initial scene memory carries compressed previous scene
            scene_memory = SceneMemory(
                scene_id=scene.scene_id,
                previous_scene_summary=(
                    previous_scene.summary[:300] if previous_scene else ""
                ),
            )

            # Analyze all frames in scene (memory updates happen inside)
            eyes_results, final_scene_memory = await self._analyzer.analyze_scene(
                scene,
                scene_frames,
                temp_dir,
                video_memory=video_memory,
                scene_memory=scene_memory,
            )
            frames_analyzed += len(eyes_results)

            resolved_scene_memory = final_scene_memory or scene_memory

            # Update video memory after scene completes
            if self._memory is not None:
                video_memory = await self._memory.update_video_memory(
                    resolved_scene_memory,
                    video_memory,
                    previous_scene,
                )

            scene_analyses.append(
                SceneAnalysis(
                    scene=scene,
                    eyes_results=eyes_results,
                    scene_memory=resolved_scene_memory,
                ),
            )
            previous_scene = resolved_scene_memory

            logger.info(
                "vd_scene_done",
                scene_id=scene.scene_id,
                frames=len(eyes_results),
                scene_type=resolved_scene_memory.scene_type,
                importance=resolved_scene_memory.importance,
            )

        logger.info(
            "vd_pipeline_done",
            scenes=len(scene_analyses),
            frames_analyzed=frames_analyzed,
            video_memory_len=len(video_memory.text),
        )

        return VDResult(
            scenes=scene_analyses,
            video_memory=video_memory,
            frames_total=len(sampling.frames),
            frames_analyzed=frames_analyzed,
            model=self._analyzer._model,
        )
