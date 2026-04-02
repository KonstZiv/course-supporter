"""Tests for VD memory pipeline (code-based merge + parsing, no API)."""

from __future__ import annotations

from course_supporter.vd.memory_pipeline import (
    MemoryPipeline,
    _extract_element_text,
    _merge_code_texts,
    build_instant_memory,
)
from course_supporter.vd.schemas import (
    EyesResult,
    MergeMethod,
    Scene,
)


def _make_eyes(
    frame_id: str,
    response: str,
    *,
    scene_id: int = 0,
    ts: float = 0.0,
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
    )


def _make_scene(scene_id: int = 0, n_frames: int = 1) -> Scene:
    return Scene(
        scene_id=scene_id,
        frame_ids=[f"f{i}" for i in range(n_frames)],
        start_sec=0.0,
        end_sec=10.0,
    )


class TestExtractElementText:
    """Test Markdown element extraction."""

    def test_extracts_elements(self) -> None:
        text = (
            "## Scene Composition\nSome header info\n"
            "## Element 1: Code (center)\n"
            "```python\nprint('hello')\n```\n"
            "## Element 2: Title (top)\n"
            "Intro to Python\n"
        )
        result = _extract_element_text(text)
        assert "print('hello')" in result
        assert "Intro to Python" in result

    def test_fallback_on_no_elements(self) -> None:
        text = "Just some plain text response"
        result = _extract_element_text(text)
        assert result == text


class TestMergeCodeTexts:
    """Test code-based overlap merge."""

    def test_overlap_merge(self) -> None:
        base = "line1\nline2\nline3"
        new = "line2\nline3\nline4"
        result = _merge_code_texts(base, new)
        assert result == "line1\nline2\nline3\nline4"

    def test_no_overlap(self) -> None:
        base = "aaa\nbbb"
        new = "ccc\nddd"
        result = _merge_code_texts(base, new)
        assert "--- next frame ---" in result
        assert "aaa" in result
        assert "ccc" in result

    def test_empty_new(self) -> None:
        assert _merge_code_texts("base", "") == "base"

    def test_empty_base(self) -> None:
        assert _merge_code_texts("", "new") == "new"

    def test_full_overlap(self) -> None:
        text = "line1\nline2"
        result = _merge_code_texts(text, text)
        assert result == text


class TestBuildInstantMemory:
    """Test instant memory construction."""

    def test_single_frame(self) -> None:
        scene = _make_scene(n_frames=1)
        eyes = [_make_eyes("f0", "## Element 1: X\ncode here")]
        im = build_instant_memory(scene, eyes)
        assert im.method == MergeMethod.SINGLE
        assert im.frame_count == 1
        assert "code here" in im.merged_text

    def test_code_diff_merge(self) -> None:
        scene = _make_scene(n_frames=2)
        eyes = [
            _make_eyes("f0", "## Element 1: X\nline1\nline2"),
            _make_eyes("f1", "## Element 1: X\nline2\nline3"),
        ]
        im = build_instant_memory(scene, eyes)
        assert im.method == MergeMethod.CODE_DIFF
        assert "line1" in im.merged_text
        assert "line3" in im.merged_text
        assert im.frame_count == 2

    def test_fallback_on_no_overlap(self) -> None:
        scene = _make_scene(n_frames=3)
        eyes = [
            _make_eyes("f0", "## Element 1: X\naaa"),
            _make_eyes("f1", "## Element 1: X\nbbb"),
            _make_eyes("f2", "## Element 1: X\nccc"),
        ]
        im = build_instant_memory(scene, eyes)
        # 2 separators for 3 frames → > 3//2=1 → fallback
        assert im.method == MergeMethod.CODE_DIFF_FALLBACK

    def test_single_frame_plain_text(self) -> None:
        """Non-Element response falls back to full text."""
        scene = _make_scene(n_frames=1)
        eyes = [_make_eyes("f0", "Just plain description")]
        im = build_instant_memory(scene, eyes)
        assert im.method == MergeMethod.SINGLE
        assert im.merged_text == "Just plain description"


class TestParseSceneMemory:
    """Test SceneMemory JSON parsing."""

    def test_valid_json(self) -> None:
        raw = (
            "```json\n"
            '{"scene_type": "screen_recording", '
            '"summary": "Demo scene", '
            '"complete_text": "code", '
            '"topics": ["python", "types"], '
            '"importance": 4}\n'
            "```"
        )
        sm = MemoryPipeline._parse_scene_memory(0, raw)
        assert sm.scene_type == "screen_recording"
        assert sm.summary == "Demo scene"
        assert sm.topics == ["python", "types"]
        assert sm.importance == 4

    def test_json_no_fences(self) -> None:
        raw = '{"scene_type": "slide", "summary": "Intro", "importance": 2}'
        sm = MemoryPipeline._parse_scene_memory(1, raw)
        assert sm.scene_type == "slide"
        assert sm.importance == 2

    def test_invalid_json_fallback(self) -> None:
        raw = "Not JSON at all, just text"
        sm = MemoryPipeline._parse_scene_memory(5, raw)
        assert sm.scene_type == "unknown"
        assert sm.summary == raw[:200]

    def test_importance_clamped(self) -> None:
        raw = '{"scene_type": "x", "summary": "y", "importance": 10}'
        sm = MemoryPipeline._parse_scene_memory(0, raw)
        assert sm.importance == 5

        raw2 = '{"scene_type": "x", "summary": "y", "importance": -1}'
        sm2 = MemoryPipeline._parse_scene_memory(0, raw2)
        assert sm2.importance == 1
