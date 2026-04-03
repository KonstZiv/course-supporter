"""Tests for VD streaming memory pipeline (no API calls)."""

from __future__ import annotations

from course_supporter.vd.memory_pipeline import (
    MemoryPipeline,
    _compress_description,
    build_instant_memory,
)
from course_supporter.vd.schemas import (
    EyesResult,
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
