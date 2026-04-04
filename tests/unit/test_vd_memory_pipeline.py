"""Tests for VD streaming memory pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.vd.memory_pipeline import (
    MemoryPipeline,
    _compress_description,
    _load_template,
    append_delta_to_scene,
    build_instant_memory,
    needs_llm_scene_update,
)
from course_supporter.vd.schemas import (
    EyesResult,
    InstantMemory,
    Scene,
    SceneMemory,
    VideoMemory,
)


def _make_eyes(
    frame_id: str,
    *,
    description: str = "Code editor with Python",
    response: str = "## Scene Composition\n**Setting:** screen_recording",
    scene_id: int = 0,
    ts: float = 0.0,
    is_delta: bool = False,
) -> EyesResult:
    return EyesResult(
        frame_id=frame_id,
        timestamp_sec=ts,
        scene_id=scene_id,
        response=response,
        n_images=1,
        latency_sec=1.0,
        input_tokens=100,
        output_tokens=50,
        description=description,
        is_delta=is_delta,
    )


class TestCompressDescription:
    """Test description compression."""

    def test_short_text_unchanged(self) -> None:
        assert _compress_description("short text") == "short text"

    def test_long_text_truncated(self) -> None:
        long = "word " * 100
        result = _compress_description(long, max_len=50)
        assert len(result) <= 55  # some tolerance for word boundaries

    def test_multiline_collapsed(self) -> None:
        text = "line one\nline two\nline three"
        result = _compress_description(text)
        assert "line one" in result
        assert "\n" not in result

    def test_empty_lines_skipped(self) -> None:
        text = "\n\nline one\n\n\nline two\n\n"
        result = _compress_description(text)
        assert result.startswith("line one")


class TestBuildInstantMemory:
    """Test rolling 2-frame instant memory."""

    def test_first_frame_no_previous(self) -> None:
        eyes = _make_eyes("f0", description="Code editor open")
        im = build_instant_memory(eyes, None)
        assert im.frame_id == "f0"
        assert im.current == "Code editor open"
        assert im.previous == ""
        assert im.is_delta is False

    def test_second_frame_has_previous(self) -> None:
        eyes0 = _make_eyes("f0", description="Code editor open")
        im0 = build_instant_memory(eyes0, None)

        eyes1 = _make_eyes(
            "f1",
            description="New function added",
            is_delta=True,
            ts=2.0,
        )
        im1 = build_instant_memory(eyes1, im0)
        assert im1.frame_id == "f1"
        assert im1.current == "New function added"
        assert "Code editor open" in im1.previous
        assert im1.is_delta is True

    def test_rolling_window_only_keeps_one_previous(self) -> None:
        eyes0 = _make_eyes("f0", description="Frame zero")
        im0 = build_instant_memory(eyes0, None)

        eyes1 = _make_eyes("f1", description="Frame one")
        im1 = build_instant_memory(eyes1, im0)

        eyes2 = _make_eyes("f2", description="Frame two")
        im2 = build_instant_memory(eyes2, im1)

        assert im2.current == "Frame two"
        assert "Frame one" in im2.previous
        # Frame zero should NOT be in previous (only 1-frame lookback)
        assert "Frame zero" not in im2.previous

    def test_previous_compressed(self) -> None:
        long_desc = "A " * 500  # 1000 chars
        eyes0 = _make_eyes("f0", description=long_desc)
        im0 = build_instant_memory(eyes0, None)

        eyes1 = _make_eyes("f1", description="Short")
        im1 = build_instant_memory(eyes1, im0)
        assert len(im1.previous) <= 300

    def test_preserves_scene_id(self) -> None:
        eyes = _make_eyes("f0", description="X", scene_id=5)
        im = build_instant_memory(eyes, None)
        assert im.scene_id == 5

    def test_preserves_timestamp(self) -> None:
        eyes = _make_eyes("f0", description="X", ts=42.5)
        im = build_instant_memory(eyes, None)
        assert im.timestamp_sec == 42.5


def _make_instant(
    *,
    is_delta: bool = False,
    current: str = "short",
) -> InstantMemory:
    return InstantMemory(
        frame_id="f0",
        scene_id=0,
        timestamp_sec=0.0,
        current=current,
        previous="",
        is_delta=is_delta,
    )


class TestNeedsLlmSceneUpdate:
    """Test delta accumulation thresholds."""

    def test_full_frame_always_needs_llm(self) -> None:
        instant = _make_instant(is_delta=False)
        assert needs_llm_scene_update(instant, 0, 0) is True

    def test_short_delta_no_llm(self) -> None:
        instant = _make_instant(is_delta=True, current="cursor moved")
        assert needs_llm_scene_update(instant, 0, 0) is False

    def test_accumulated_delta_over_threshold(self) -> None:
        instant = _make_instant(is_delta=True, current="x" * 50)
        # 60 accumulated + 50 new = 110 > 100
        assert needs_llm_scene_update(instant, 60, 2) is True

    def test_single_long_delta_triggers_llm(self) -> None:
        instant = _make_instant(is_delta=True, current="x" * 200)
        assert needs_llm_scene_update(instant, 0, 0) is True

    def test_5th_consecutive_delta_triggers_llm(self) -> None:
        instant = _make_instant(is_delta=True, current="tiny")
        # 4 deltas already, this is the 5th
        assert needs_llm_scene_update(instant, 20, 4) is True

    def test_4th_delta_no_trigger(self) -> None:
        instant = _make_instant(is_delta=True, current="tiny")
        assert needs_llm_scene_update(instant, 20, 3) is False


class TestAppendDeltaToScene:
    """Test mechanical delta appending."""

    def test_append_to_empty(self) -> None:
        sm = SceneMemory(scene_id=0)
        result = append_delta_to_scene(sm, "cursor moved")
        assert "cursor moved" in result.summary
        assert result.frames_seen == 1

    def test_append_to_existing(self) -> None:
        sm = SceneMemory(scene_id=0, summary="Code editor open", frames_seen=1)
        result = append_delta_to_scene(sm, "line added")
        assert result.summary == "Code editor open | line added"
        assert result.frames_seen == 2

    def test_preserves_fields(self) -> None:
        sm = SceneMemory(
            scene_id=5,
            summary="Existing",
            scene_type="screen_recording",
            topics=["python"],
            importance=4,
            previous_scene_summary="prev",
        )
        result = append_delta_to_scene(sm, "delta")
        assert result.scene_id == 5
        assert result.scene_type == "screen_recording"
        assert result.topics == ["python"]
        assert result.importance == 4
        assert result.previous_scene_summary == "prev"


class TestParseSceneMemory:
    """Test SceneMemory JSON parsing."""

    def test_valid_json(self) -> None:
        raw = (
            "```json\n"
            '{"scene_type": "screen_recording", '
            '"summary": "Demo scene", '
            '"topics": ["python", "types"], '
            '"importance": 4}\n'
            "```"
        )
        sm = MemoryPipeline._parse_scene_memory(
            scene_id=0,
            raw=raw,
            frames_seen=3,
            previous_scene_summary="prev",
        )
        assert sm.scene_type == "screen_recording"
        assert sm.summary == "Demo scene"
        assert sm.topics == ["python", "types"]
        assert sm.importance == 4
        assert sm.frames_seen == 3
        assert sm.previous_scene_summary == "prev"

    def test_json_no_fences(self) -> None:
        raw = '{"scene_type": "slide", "summary": "Intro", "importance": 2}'
        sm = MemoryPipeline._parse_scene_memory(
            scene_id=1,
            raw=raw,
            frames_seen=1,
            previous_scene_summary="",
        )
        assert sm.scene_type == "slide"
        assert sm.importance == 2

    def test_invalid_json_fallback(self) -> None:
        raw = "Not JSON at all, just text"
        sm = MemoryPipeline._parse_scene_memory(
            scene_id=5,
            raw=raw,
            frames_seen=2,
            previous_scene_summary="",
        )
        assert sm.scene_type == "unknown"
        assert sm.summary == raw[:200]

    def test_importance_clamped(self) -> None:
        raw = '{"scene_type": "x", "summary": "y", "importance": 10}'
        sm = MemoryPipeline._parse_scene_memory(
            scene_id=0,
            raw=raw,
            frames_seen=1,
            previous_scene_summary="",
        )
        assert sm.importance == 5

        raw2 = '{"scene_type": "x", "summary": "y", "importance": -1}'
        sm2 = MemoryPipeline._parse_scene_memory(
            scene_id=0,
            raw=raw2,
            frames_seen=1,
            previous_scene_summary="",
        )
        assert sm2.importance == 1

    def test_frames_seen_preserved(self) -> None:
        raw = '{"scene_type": "x", "summary": "y"}'
        sm = MemoryPipeline._parse_scene_memory(
            scene_id=0,
            raw=raw,
            frames_seen=7,
            previous_scene_summary="ctx",
        )
        assert sm.frames_seen == 7
        assert sm.previous_scene_summary == "ctx"


# -- Fixtures for LLM-mocked tests ----------------------------------------


@pytest.fixture()
def mock_key_pool() -> MagicMock:
    pool = MagicMock()
    pool.__len__ = lambda _: 1
    pool.next_key.return_value = MagicMock(get_secret_value=lambda: "fake-key")
    return pool


@pytest.fixture()
def pipeline(mock_key_pool: MagicMock) -> MemoryPipeline:
    return MemoryPipeline(mock_key_pool, rpm_per_key=999)


# -- _load_template --------------------------------------------------------


class TestLoadTemplate:
    def test_loads_scene_memory_template(self) -> None:
        t = _load_template("scene_memory_streaming.txt")
        assert "{scene_id}" in t

    def test_loads_video_memory_template(self) -> None:
        t = _load_template("video_memory.txt")
        assert "{current_video_memory}" in t

    def test_caches_result(self) -> None:
        t1 = _load_template("video_memory.txt")
        t2 = _load_template("video_memory.txt")
        assert t1 is t2


# -- MemoryPipeline.__init__ -----------------------------------------------


class TestMemoryPipelineInit:
    def test_min_interval_calculation(self, mock_key_pool: MagicMock) -> None:
        mp = MemoryPipeline(mock_key_pool, rpm_per_key=10)
        # 1 key * 10 rpm = 10 rpm total => 6s interval
        assert mp._min_interval == pytest.approx(6.0)

    def test_model_stored(self, mock_key_pool: MagicMock) -> None:
        mp = MemoryPipeline(mock_key_pool, model="test-model")
        assert mp._model == "test-model"

    def test_fallback_model_stored(self, mock_key_pool: MagicMock) -> None:
        mp = MemoryPipeline(mock_key_pool, fallback_model="fallback-model")
        assert mp._fallback_model == "fallback-model"


# -- update_scene_memory (mocked LLM) -------------------------------------


class TestUpdateSceneMemory:
    async def test_returns_parsed_scene_memory(self, pipeline: MemoryPipeline) -> None:
        instant = InstantMemory(
            frame_id="f0",
            scene_id=0,
            timestamp_sec=10.0,
            current="Code editor with Python",
            previous="",
            is_delta=False,
        )
        sm = SceneMemory(scene_id=0)

        mock_response = MagicMock(
            text='{"scene_type": "screen_recording", '
            '"summary": "IDE open", "topics": ["python"], '
            '"importance": 3}'
        )
        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value=mock_response.text),
        ):
            result = await pipeline.update_scene_memory(instant, sm)

        assert result.scene_type == "screen_recording"
        assert result.summary == "IDE open"
        assert result.frames_seen == 1

    async def test_increments_frames_seen(self, pipeline: MemoryPipeline) -> None:
        instant = InstantMemory(
            frame_id="f1",
            scene_id=0,
            timestamp_sec=20.0,
            current="New line added",
            previous="Code editor",
            is_delta=True,
        )
        sm = SceneMemory(scene_id=0, summary="Editor open", frames_seen=3)

        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value='{"scene_type": "x", "summary": "y"}'),
        ):
            result = await pipeline.update_scene_memory(instant, sm)

        assert result.frames_seen == 4


# -- update_video_memory (mocked LLM) -------------------------------------


class TestUpdateVideoMemory:
    async def test_returns_updated_video_memory(self, pipeline: MemoryPipeline) -> None:
        sm = SceneMemory(
            scene_id=0,
            summary="Python demo",
            scene_type="screen_recording",
            topics=["python"],
        )
        vm = VideoMemory()

        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value="Updated video context"),
        ):
            result = await pipeline.update_video_memory(sm, vm, None)

        assert result.text == "Updated video context"
        assert result.scenes_processed == 1

    async def test_increments_scenes_processed(self, pipeline: MemoryPipeline) -> None:
        sm = SceneMemory(scene_id=1, summary="Next scene")
        vm = VideoMemory(text="Previous", scenes_processed=2)

        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value="More context"),
        ):
            result = await pipeline.update_video_memory(sm, vm, None)

        assert result.scenes_processed == 3

    async def test_includes_previous_scene(self, pipeline: MemoryPipeline) -> None:
        sm = SceneMemory(scene_id=1, summary="Current")
        vm = VideoMemory()
        prev = SceneMemory(scene_id=0, summary="Previous scene content")

        call_args: list[str] = []

        async def capture_prompt(prompt: str) -> str:
            call_args.append(prompt)
            return "result"

        with patch.object(pipeline, "_call_llm", new=capture_prompt):
            await pipeline.update_video_memory(sm, vm, prev)

        assert "Previous scene content" in call_args[0]


# -- process_scene (mocked LLM) -------------------------------------------


class TestProcessScene:
    async def test_full_scene_processing(self, pipeline: MemoryPipeline) -> None:
        scene = Scene(scene_id=0, frame_ids=["f0", "f1"], start_sec=0.0, end_sec=10.0)
        eyes_results = [
            _make_eyes("f0", ts=0.0, description="Frame zero"),
            _make_eyes("f1", ts=5.0, description="Frame one"),
        ]
        vm = VideoMemory()

        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value='{"scene_type": "x", "summary": "y"}'),
        ):
            final_sm, final_vm = await pipeline.process_scene(scene, eyes_results, vm)

        assert final_sm.scene_id == 0
        assert final_sm.frames_seen == 2
        assert final_vm.scenes_processed == 1

    async def test_process_scene_with_previous(self, pipeline: MemoryPipeline) -> None:
        scene = Scene(scene_id=1, frame_ids=["f0"], start_sec=10.0, end_sec=20.0)
        eyes_results = [_make_eyes("f0", ts=10.0)]
        vm = VideoMemory(text="Existing context", scenes_processed=1)
        prev = SceneMemory(scene_id=0, summary="Previous scene")

        with patch.object(
            pipeline,
            "_call_llm",
            new=AsyncMock(return_value='{"scene_type": "x", "summary": "y"}'),
        ):
            final_sm, final_vm = await pipeline.process_scene(
                scene, eyes_results, vm, prev
            )

        assert final_vm.scenes_processed == 2
        assert "Previous scene" in final_sm.previous_scene_summary


# -- _call_llm (mocked Gemini) --------------------------------------------


class TestCallLlm:
    async def test_success(self, pipeline: MemoryPipeline) -> None:
        mock_response = MagicMock(text="LLM response")
        mock_client = MagicMock()
        mock_client.models.generate_content = MagicMock(return_value=mock_response)

        with patch("google.genai.Client", return_value=mock_client):
            result = await pipeline._call_llm("test prompt")

        assert result == "LLM response"

    async def test_rate_limit_retry_uses_fallback(
        self, pipeline: MemoryPipeline
    ) -> None:
        rate_err = Exception("429 Resource exhausted")
        rate_err.code = 429  # type: ignore[attr-defined]
        mock_response = MagicMock(text="Fallback OK")

        call_count = 0

        def side_effect(*_args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise rate_err
            return mock_response

        mock_client = MagicMock()
        mock_client.models.generate_content = side_effect

        with (
            patch("google.genai.Client", return_value=mock_client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await pipeline._call_llm("test")

        assert result == "Fallback OK"
        assert call_count == 2

    async def test_permanent_error_raises(self, pipeline: MemoryPipeline) -> None:
        perm_err = Exception("401 Unauthorized")
        perm_err.code = 401  # type: ignore[attr-defined]

        mock_client = MagicMock()
        mock_client.models.generate_content = MagicMock(side_effect=perm_err)

        with (
            patch("google.genai.Client", return_value=mock_client),
            pytest.raises(Exception, match="401"),
        ):
            await pipeline._call_llm("test")
