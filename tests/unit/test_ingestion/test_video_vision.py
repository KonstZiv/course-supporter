"""Unit tests for the Krok 4 Pass 1 vision chain-diff mechanism (Phase 2.4).

CI-level (no real LLM, no ffmpeg): the chunk planner, the plain-text
chunk-response parser (including the ``!= N`` and marker-mismatch retry
edges), the PiP-mask application (real cv2 round-trip on a synthetic
JPEG), and the ``run_pass_1`` orchestrator against a fake StageRouter.
Preservation / N sweet-spot / image-cap calibration is the operator-run
RUN_SMOKE spike (C6), not exercised here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video_pipeline import vision
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    DetectionResult,
    FrameKind,
    PiPMask,
    SampledFrame,
    Scene,
    VideoFileMetadata,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.stage_router import StageResult

# ── builders ─────────────────────────────────────────────────────────────


def _meta() -> VideoFileMetadata:
    return VideoFileMetadata(duration_ms=600_000, codec="h264", resolution="1280x720")


def _frame(
    position_ms: int, change_class: ChangeClass, path: Path = Path("/tmp/f.jpg")
) -> SampledFrame:
    return SampledFrame(
        frame_position_ms=position_ms, change_class=change_class, frame_path=path
    )


def _scene(scene_id: int, frames: list[SampledFrame]) -> Scene:
    return Scene(
        scene_id=scene_id,
        start_ms=frames[0].frame_position_ms,
        end_ms=frames[-1].frame_position_ms,
        frames=frames,
    )


def _pf(position_ms: int, change_class: ChangeClass, scene_id: int = 0) -> vision._PF:
    return vision._PF(sampled=_frame(position_ms, change_class), scene_id=scene_id)


def _write_jpeg(directory: Path, name: str, fill: int = 200) -> Path:
    path = directory / f"frame_{name}.jpg"
    cv2.imwrite(str(path), np.full((64, 64, 3), fill, dtype=np.uint8))
    return path


def _valid_content(render_context: dict[str, Any]) -> str:
    """A well-formed chunk response built from the render context markers."""
    return "\n".join(
        f"{f['marker']}\nDescription for {f['marker']}."
        for f in render_context["frames"]
    )


class _FakeStageRouter:
    """StageRouter double: records calls, echoes a valid chunk response."""

    def __init__(self, content_fn: Any = _valid_content) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content_fn = content_fn

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Any = None,
        contents: Any = None,
        **render_context: Any,
    ) -> StageResult:
        self.calls.append(
            {
                "stage": stage_name,
                "contents": contents,
                "render_context": render_context,
            }
        )
        content = self._content_fn(render_context)
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used="dashscope",
            model_used="qwen3-vl-32b-instruct",
            attempt_count=1,
        )


class _RaisingStageRouter:
    async def execute_for_stage(self, *args: Any, **kwargs: Any) -> StageResult:
        raise RuntimeError("ladder boom")


# ── marker + kind helpers ─────────────────────────────────────────────────


class TestMarkerAndKind:
    @pytest.mark.parametrize(
        ("ms", "expected"),
        [
            (0, "[FRAME 00:00:00]"),
            (5_000, "[FRAME 00:00:05]"),
            (83_000, "[FRAME 00:01:23]"),
            (3_661_000, "[FRAME 01:01:01]"),  # > 1 h — HH field needed (150-min cap)
        ],
    )
    def test_format_marker(self, ms: int, expected: str) -> None:
        assert vision._format_marker(ms) == expected

    def test_first_in_chunk_is_anchor_even_when_low(self) -> None:
        # index 0 forces anchor regardless of change_class.
        assert vision._kind_at(0, ChangeClass.LOW) is FrameKind.ANCHOR

    def test_scene_start_is_anchor_mid_chunk(self) -> None:
        assert vision._kind_at(3, ChangeClass.BOUNDARY) is FrameKind.ANCHOR
        assert vision._kind_at(3, ChangeClass.FIRST) is FrameKind.ANCHOR

    def test_low_medium_mid_chunk_is_diff(self) -> None:
        assert vision._kind_at(2, ChangeClass.LOW) is FrameKind.DIFF
        assert vision._kind_at(2, ChangeClass.MEDIUM) is FrameKind.DIFF


# ── chunk planning (constants monkeypatched so the algorithm, not the seed,
#    is under test) ─────────────────────────────────────────────────────────


class TestChunkPlanning:
    def test_image_cap_splits_a_long_scene(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "_IMAGE_BUDGET", 3)
        monkeypatch.setattr(vision, "_BUDGET_TOKENS", 100_000)  # token cap inactive
        frames = [_frame(0, ChangeClass.FIRST)] + [
            _frame(i * 1000, ChangeClass.LOW) for i in range(1, 7)
        ]
        chunks = vision._plan_chunks([_scene(0, frames)])

        assert [len(c) for c in chunks] == [3, 3, 1]
        # first frame of every chunk is an anchor (self-sufficient chunk).
        for chunk in chunks:
            assert vision._kind_at(0, chunk[0].sampled.change_class) is FrameKind.ANCHOR

    def test_token_budget_splits_a_long_scene(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "_IMAGE_BUDGET", 100)  # image cap inactive
        monkeypatch.setattr(vision, "_BUDGET_TOKENS", 1000)
        monkeypatch.setattr(vision, "_EST_ANCHOR_TOKENS", 700)
        monkeypatch.setattr(vision, "_EST_DIFF_TOKENS", 200)
        # anchor(700) + diff(200) = 900 fits; a second diff (1100) overflows.
        frames = [_frame(0, ChangeClass.FIRST)] + [
            _frame(i * 1000, ChangeClass.LOW) for i in range(1, 6)
        ]
        chunks = vision._plan_chunks([_scene(0, frames)])

        assert [len(c) for c in chunks] == [2, 2, 2]

    def test_small_scenes_merge_into_one_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "_IMAGE_BUDGET", 10)
        monkeypatch.setattr(vision, "_BUDGET_TOKENS", 100_000)
        scenes = [
            _scene(
                i,
                [
                    _frame(i * 100, ChangeClass.BOUNDARY if i else ChangeClass.FIRST),
                    _frame(i * 100 + 50, ChangeClass.LOW),
                ],
            )
            for i in range(3)
        ]
        chunks = vision._plan_chunks(scenes)

        assert len(chunks) == 1
        assert len(chunks[0]) == 6
        # scene_ids are preserved through the flatten.
        assert [pf.scene_id for pf in chunks[0]] == [0, 0, 1, 1, 2, 2]

    def test_scenes_do_not_merge_past_image_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vision, "_IMAGE_BUDGET", 3)
        monkeypatch.setattr(vision, "_BUDGET_TOKENS", 100_000)
        scenes = [
            _scene(0, [_frame(0, ChangeClass.FIRST), _frame(50, ChangeClass.LOW)]),
            _scene(
                1, [_frame(100, ChangeClass.BOUNDARY), _frame(150, ChangeClass.LOW)]
            ),
        ]
        chunks = vision._plan_chunks(scenes)

        assert [len(c) for c in chunks] == [2, 2]


# ── chunk-response parsing ─────────────────────────────────────────────────


class TestParseChunk:
    def test_parses_blocks_ordinally_with_kinds(self) -> None:
        chunk = [_pf(0, ChangeClass.FIRST), _pf(5_000, ChangeClass.LOW)]
        content = (
            "[FRAME 00:00:00]\nFull anchor description.\n"
            "[FRAME 00:00:05]\nOnly the delta."
        )
        result = vision._parse_chunk(content, chunk)

        assert [d.frame_position_ms for d in result] == [0, 5_000]
        assert result[0].kind is FrameKind.ANCHOR
        assert result[0].description == "Full anchor description."
        assert result[1].kind is FrameKind.DIFF
        assert result[1].description == "Only the delta."
        assert result[1].scene_id == 0

    def test_wrong_block_count_raises_structural_retry(self) -> None:
        chunk = [_pf(0, ChangeClass.FIRST), _pf(5_000, ChangeClass.LOW)]
        content = "[FRAME 00:00:00]\nOnly one block, two expected."

        with pytest.raises(StructuralRetryError, match="expected 2"):
            vision._parse_chunk(content, chunk)

    def test_marker_mismatch_raises_structural_retry(self) -> None:
        chunk = [_pf(0, ChangeClass.FIRST), _pf(5_000, ChangeClass.LOW)]
        # second marker should be 00:00:05 but the model emitted 00:00:09.
        content = "[FRAME 00:00:00]\nAnchor.\n[FRAME 00:00:09]\nWrong marker."

        with pytest.raises(StructuralRetryError, match="marker mismatch"):
            vision._parse_chunk(content, chunk)


# ── PiP masking ────────────────────────────────────────────────────────────


class TestPipMask:
    def test_masks_region_and_preserves_outside(self, tmp_path: Path) -> None:
        cv2.imwrite(
            str(tmp_path / "f.jpg"), np.full((100, 200, 3), 255, dtype=np.uint8)
        )
        mask = PiPMask(x=10, y=10, width=40, height=30, confidence=0.9)

        out = vision._read_masked_jpeg(tmp_path / "f.jpg", mask)
        decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)

        # well inside the box → grey ~128 (JPEG is lossy → tolerance).
        assert abs(int(decoded[22, 28, 0]) - vision._PIP_FILL_GRAY) < 12
        # well outside the box → original white preserved.
        assert int(decoded[80, 180, 0]) > 230

    def test_none_mask_keeps_image(self, tmp_path: Path) -> None:
        cv2.imwrite(str(tmp_path / "f.jpg"), np.full((40, 40, 3), 255, dtype=np.uint8))

        out = vision._read_masked_jpeg(tmp_path / "f.jpg", None)
        decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)

        assert int(decoded[20, 20, 0]) > 230

    def test_out_of_bounds_box_is_clamped(self, tmp_path: Path) -> None:
        cv2.imwrite(str(tmp_path / "f.jpg"), np.full((40, 40, 3), 255, dtype=np.uint8))
        # box extends past the frame — clamp must not raise / overrun.
        mask = PiPMask(x=30, y=30, width=100, height=100, confidence=0.5)

        out = vision._read_masked_jpeg(tmp_path / "f.jpg", mask)
        decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)

        assert abs(int(decoded[35, 35, 0]) - vision._PIP_FILL_GRAY) < 12

    def test_unreadable_frame_raises_processing_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.jpg"

        with pytest.raises(ProcessingError, match="cannot read frame"):
            vision._read_masked_jpeg(missing, None)


# ── run_pass_1 orchestrator ────────────────────────────────────────────────


class TestRunPass1:
    async def test_describes_chunks_and_wires_the_stage_call(
        self, tmp_path: Path
    ) -> None:
        frames = [
            _frame(0, ChangeClass.FIRST, _write_jpeg(tmp_path, "0")),
            _frame(5_000, ChangeClass.LOW, _write_jpeg(tmp_path, "5")),
        ]
        detection = DetectionResult(scenes=[_scene(0, frames)], pip_mask=None)
        router = _FakeStageRouter()

        result = await vision.run_pass_1(detection, _meta(), stage_router=router)

        assert [d.frame_position_ms for d in result] == [0, 5_000]
        assert result[0].kind is FrameKind.ANCHOR
        assert result[1].kind is FrameKind.DIFF

        assert len(router.calls) == 1
        call = router.calls[0]
        assert call["stage"] == "video_pass_1_vision"
        assert len(call["contents"]) == 2  # one masked JPEG per frame
        assert all(isinstance(b, bytes) for b in call["contents"])
        assert call["render_context"]["n_frames"] == 2
        assert call["render_context"]["frames"][0]["marker"] == "[FRAME 00:00:00]"
        assert call["render_context"]["frames"][0]["kind"] == "ANCHOR"
        assert call["render_context"]["frames"][1]["kind"] == "DIFF"

    async def test_empty_scenes_returns_empty(self) -> None:
        detection = DetectionResult(scenes=[], pip_mask=None)

        result = await vision.run_pass_1(
            detection, _meta(), stage_router=_FakeStageRouter()
        )

        assert result == []

    async def test_chunk_failure_is_fail_fast_processing_error(
        self, tmp_path: Path
    ) -> None:
        frames = [_frame(0, ChangeClass.FIRST, _write_jpeg(tmp_path, "0"))]
        detection = DetectionResult(scenes=[_scene(0, frames)], pip_mask=None)

        with pytest.raises(ProcessingError, match="chunk 0"):
            await vision.run_pass_1(
                detection, _meta(), stage_router=_RaisingStageRouter()
            )
