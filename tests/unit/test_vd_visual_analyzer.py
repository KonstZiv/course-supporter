"""Tests for VisualAnalyzer (Eyes step) — no real API calls."""

from __future__ import annotations

from course_supporter.vd.schemas import ChangeClass, DeltaStrategy, EyesResult
from course_supporter.vd.visual_analyzer import (
    VisualAnalyzer,
    _is_conditional_delta,
    _load_prompt,
    _parse_response,
)


class TestLoadPrompt:
    """Test prompt loading and caching."""

    def test_loads_full_prompt(self) -> None:
        prompt = _load_prompt("eyes_v3.txt")
        assert "## Scene Composition" in prompt
        assert "{course_context_block}" in prompt

    def test_loads_delta_prompt(self) -> None:
        prompt = _load_prompt("eyes_v3_delta.txt")
        assert "{previous_description}" in prompt
        assert "ONLY what changed" in prompt

    def test_loads_conditional_prompt(self) -> None:
        prompt = _load_prompt("eyes_v3_conditional.txt")
        assert "{previous_description}" in prompt
        assert "CHANGES ONLY:" in prompt
        assert "## Scene Composition" in prompt

    def test_cached(self) -> None:
        p1 = _load_prompt("eyes_v3.txt")
        p2 = _load_prompt("eyes_v3.txt")
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
        assert len(desc) <= 300

    def test_fallback_on_empty(self) -> None:
        desc, scene_type, importance = _parse_response("Some random text")
        assert desc == "Some random text"[:200]
        assert scene_type == ""
        assert importance == 3

    def test_delta_response_parsed(self) -> None:
        """Delta responses still get description from raw text."""
        text = "Lines 3-5 are now highlighted in blue.\nNew output appeared."
        desc, _, _ = _parse_response(text)
        assert "highlighted" in desc


class TestIsConditionalDelta:
    """Test conditional delta detection."""

    def test_detects_changes_only(self) -> None:
        assert _is_conditional_delta("CHANGES ONLY: cursor moved")
        assert _is_conditional_delta("changes only: minor update")
        assert _is_conditional_delta("Changes Only:\nLine 5 highlighted")

    def test_rejects_full_response(self) -> None:
        assert not _is_conditional_delta("## Scene Composition\n...")
        assert not _is_conditional_delta("The frame shows a code editor")


class TestBuildPrompt:
    """Test prompt assembly with context blocks and delta strategies."""

    def test_full_no_context(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        prompt = analyzer._build_prompt("", [], None)
        assert "## Scene Composition" in prompt
        assert "Course context" not in prompt

    def test_full_with_course_context(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        prompt = analyzer._build_prompt("Topics covered", [], None)
        assert "Course context (what has been covered):" in prompt
        assert "Topics covered" in prompt

    def test_full_with_scene_context(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        ctx = [
            {"ts": 10.0, "desc": "Code editor"},
            {"ts": 12.0, "desc": "New function"},
        ]
        prompt = analyzer._build_prompt("", ctx, None)
        assert "Previous frames" in prompt
        assert "10s: Code editor" in prompt

    def test_explicit_delta_with_prev(self) -> None:
        """Explicit strategy uses delta prompt when prev_result given."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.EXPLICIT
        prev = EyesResult(
            frame_id="f0",
            timestamp_sec=0.0,
            scene_id=0,
            response="## Scene Composition\nFull description here",
            n_images=1,
            latency_sec=1.0,
            input_tokens=100,
            output_tokens=50,
        )
        prompt = analyzer._build_prompt("", [], prev)
        assert "ONLY what changed" in prompt
        assert "Full description here" in prompt

    def test_conditional_with_prev(self) -> None:
        """Conditional strategy uses conditional prompt."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.CONDITIONAL
        prev = EyesResult(
            frame_id="f0",
            timestamp_sec=0.0,
            scene_id=0,
            response="Previous content",
            n_images=1,
            latency_sec=1.0,
            input_tokens=100,
            output_tokens=50,
        )
        prompt = analyzer._build_prompt("", [], prev)
        assert "CHANGES ONLY:" in prompt
        assert "Previous content" in prompt

    def test_none_strategy_ignores_prev(self) -> None:
        """NONE strategy always uses full prompt."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        # NONE strategy: prev_result is never passed to _build_prompt
        prompt = analyzer._build_prompt("", [], None)
        assert "## Scene Composition" in prompt

    def test_scene_context_max_5(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        ctx = [{"ts": float(i), "desc": f"d{i}"} for i in range(10)]
        prompt = analyzer._build_prompt("", ctx, None)
        assert "d5" in prompt
        assert "d9" in prompt
        assert "d4" not in prompt


class TestShouldUseDelta:
    """Test delta decision logic."""

    def _make_analyzer(
        self,
        strategy: DeltaStrategy,
    ) -> VisualAnalyzer:
        a = VisualAnalyzer.__new__(VisualAnalyzer)
        a._delta_strategy = strategy
        return a

    def _make_prev(self) -> EyesResult:
        return EyesResult(
            frame_id="f0",
            timestamp_sec=0.0,
            scene_id=0,
            response="prev",
            n_images=1,
            latency_sec=1.0,
            input_tokens=100,
            output_tokens=50,
        )

    def _make_frame(self, cc: ChangeClass) -> object:
        """Minimal object with change_class attribute."""
        from unittest.mock import MagicMock

        f = MagicMock()
        f.change_class = cc
        return f

    def test_none_never_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.NONE)
        assert not a._should_use_delta(
            self._make_frame(ChangeClass.LOW),
            self._make_prev(),  # type: ignore[arg-type]
        )

    def test_no_prev_never_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            self._make_frame(ChangeClass.LOW),
            None,  # type: ignore[arg-type]
        )

    def test_explicit_low_is_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert a._should_use_delta(
            self._make_frame(ChangeClass.LOW),
            self._make_prev(),  # type: ignore[arg-type]
        )

    def test_explicit_medium_is_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert a._should_use_delta(
            self._make_frame(ChangeClass.MEDIUM),
            self._make_prev(),  # type: ignore[arg-type]
        )

    def test_explicit_boundary_is_full(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            self._make_frame(ChangeClass.BOUNDARY),
            self._make_prev(),  # type: ignore[arg-type]
        )

    def test_explicit_first_is_full(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            self._make_frame(ChangeClass.FIRST),
            self._make_prev(),  # type: ignore[arg-type]
        )

    def test_conditional_always_with_prev(self) -> None:
        a = self._make_analyzer(DeltaStrategy.CONDITIONAL)
        assert a._should_use_delta(
            self._make_frame(ChangeClass.BOUNDARY),
            self._make_prev(),  # type: ignore[arg-type]
        )
