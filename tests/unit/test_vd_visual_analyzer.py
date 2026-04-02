"""Tests for VisualAnalyzer (Eyes step) — no real API calls."""

from __future__ import annotations

from course_supporter.vd.visual_analyzer import _load_prompt, _parse_response


class TestLoadPrompt:
    """Test prompt loading and caching."""

    def test_loads_prompt(self) -> None:
        prompt = _load_prompt()
        assert "## Scene Composition" in prompt
        assert "**Setting:**" in prompt
        assert "{course_context_block}" in prompt
        assert "{scene_context_block}" in prompt

    def test_cached(self) -> None:
        p1 = _load_prompt()
        p2 = _load_prompt()
        assert p1 is p2


class TestParseResponse:
    """Test Markdown response parsing."""

    def test_extracts_scene_type(self) -> None:
        text = (
            "## Scene Composition\n\n"
            "**Setting:** <screen_recording>\n\n"
            "**People:** None\n\n"
            "**Elements:**\n"
            "1. **code_area** (editor): main code\n"
        )
        _desc, scene_type, importance = _parse_response(text)
        assert scene_type == "<screen_recording>"
        assert importance == 3

    def test_extracts_description(self) -> None:
        text = (
            "## Scene Composition\n\n"
            "**Setting:** <mixed>\n\n"
            "**People:** One person in overlay\n\n"
            "**Elements:**\n"
            "1. **text_area**: slide title\n\n"
            "## Element 1: Slide (center)\n"
            "Content here\n"
        )
        desc, _, _ = _parse_response(text)
        assert "Setting" in desc
        assert "People" in desc
        assert len(desc) <= 300

    def test_truncates_long_description(self) -> None:
        long_text = "## Scene Composition\n" + "x" * 500 + "\n## Done"
        desc, _, _ = _parse_response(long_text)
        assert len(desc) <= 300

    def test_fallback_on_empty(self) -> None:
        desc, scene_type, importance = _parse_response("Some random text")
        assert desc == "Some random text"[:200]
        assert scene_type == ""
        assert importance == 3

    def test_mixed_setting(self) -> None:
        text = (
            "## Scene Composition\n\n"
            "**Setting:** <mixed> (Screen recording with speaker)\n"
        )
        _desc, scene_type, _ = _parse_response(text)
        assert "<mixed>" in scene_type
        assert "speaker" in scene_type


class TestBuildPrompt:
    """Test prompt assembly with context blocks."""

    def test_no_context(self) -> None:
        from course_supporter.vd.visual_analyzer import VisualAnalyzer

        prompt = VisualAnalyzer._build_prompt("", [])
        assert "Course context" not in prompt
        assert "Previous frames" not in prompt
        assert "## Scene Composition" in prompt

    def test_with_course_context(self) -> None:
        from course_supporter.vd.visual_analyzer import VisualAnalyzer

        prompt = VisualAnalyzer._build_prompt("Topics covered so far", [])
        assert "Course context (what has been covered):" in prompt
        assert "Topics covered so far" in prompt

    def test_with_scene_context(self) -> None:
        from course_supporter.vd.visual_analyzer import VisualAnalyzer

        ctx = [
            {"ts": 10.0, "desc": "Code editor visible"},
            {"ts": 12.0, "desc": "New function defined"},
        ]
        prompt = VisualAnalyzer._build_prompt("", ctx)
        assert "Previous frames in this scene:" in prompt
        assert "10s: Code editor visible" in prompt
        assert "12s: New function defined" in prompt

    def test_scene_context_max_5(self) -> None:
        from course_supporter.vd.visual_analyzer import VisualAnalyzer

        ctx = [{"ts": float(i), "desc": f"desc_{i}"} for i in range(10)]
        prompt = VisualAnalyzer._build_prompt("", ctx)
        # Only last 5 should be included
        assert "desc_5" in prompt
        assert "desc_9" in prompt
        assert "desc_4" not in prompt
