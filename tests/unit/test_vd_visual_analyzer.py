"""Tests for VisualAnalyzer (Eyes step) — no real API calls."""

from __future__ import annotations

from course_supporter.vd.schemas import (
    ChangeClass,
    DeltaStrategy,
    EyesResult,
    FrameSource,
    SampledFrame,
    SceneMemory,
    VideoMemory,
)
from course_supporter.vd.visual_analyzer import (
    VisualAnalyzer,
    _build_memory_context,
    _build_similarity_hint,
    _is_conditional_delta,
    _load_prompt,
    _parse_response,
)


class TestLoadPrompt:
    """Test prompt loading and caching."""

    def test_loads_full_prompt(self) -> None:
        prompt = _load_prompt("eyes_v3.txt")
        assert "## Scene Composition" in prompt
        assert "{context_block}" in prompt

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


def _make_frame(
    *,
    change_class: ChangeClass = ChangeClass.LOW,
    dhash_dist: float = 0.07,
) -> SampledFrame:
    """Helper to create a SampledFrame for tests."""
    return SampledFrame(
        frame_id="frame_001_10s",
        filename="frame_001.jpg",
        timestamp_sec=10.0,
        scene_id=0,
        source=FrameSource.GOLDEN,
        dhash="a" * 64,
        dhash_dist=dhash_dist,
        time_gap=2.0,
        change_class=change_class,
    )


class TestBuildSimilarityHint:
    """Test similarity hint generation."""

    def test_none_frame_returns_empty(self) -> None:
        assert _build_similarity_hint(None) == ""

    def test_low_change_high_similarity(self) -> None:
        frame = _make_frame(change_class=ChangeClass.LOW, dhash_dist=0.07)
        hint = _build_similarity_hint(frame)
        assert "93%" in hint
        assert "LOW change" in hint
        assert "noisy" in hint

    def test_boundary_change_low_similarity(self) -> None:
        frame = _make_frame(change_class=ChangeClass.BOUNDARY, dhash_dist=0.45)
        hint = _build_similarity_hint(frame)
        assert "55%" in hint
        assert "HIGH change" in hint

    def test_medium_change(self) -> None:
        frame = _make_frame(change_class=ChangeClass.MEDIUM, dhash_dist=0.15)
        hint = _build_similarity_hint(frame)
        assert "85%" in hint
        assert "MEDIUM change" in hint

    def test_first_frame(self) -> None:
        frame = _make_frame(change_class=ChangeClass.FIRST, dhash_dist=0.0)
        hint = _build_similarity_hint(frame)
        assert "100%" in hint
        assert "FIRST frame" in hint


def _empty_memory() -> tuple[SceneMemory, VideoMemory]:
    """Create empty memory states for testing."""
    return SceneMemory(scene_id=0), VideoMemory()


class TestBuildMemoryContext:
    """Test memory context block generation."""

    def test_empty_memory(self) -> None:
        sm, vm = _empty_memory()
        assert _build_memory_context(None, sm, vm) == ""

    def test_video_memory_only(self) -> None:
        sm, _ = _empty_memory()
        vm = VideoMemory(text="ESP32 programming course")
        ctx = _build_memory_context(None, sm, vm)
        assert "Video context:" in ctx
        assert "ESP32 programming course" in ctx

    def test_all_three_levels(self) -> None:
        from course_supporter.vd.schemas import InstantMemory

        instant = InstantMemory(
            frame_id="f1",
            scene_id=0,
            timestamp_sec=10.0,
            current="Code editor",
            previous="Title slide",
        )
        sm = SceneMemory(scene_id=0, summary="Instructor writes code")
        vm = VideoMemory(text="Python course")
        ctx = _build_memory_context(instant, sm, vm)
        assert "Video context:" in ctx
        assert "Current scene so far:" in ctx
        assert "Previous frame:" in ctx
        assert "Title slide" in ctx

    def test_no_previous_frame(self) -> None:
        from course_supporter.vd.schemas import InstantMemory

        instant = InstantMemory(
            frame_id="f0",
            scene_id=0,
            timestamp_sec=0.0,
            current="First frame",
            previous="",
        )
        sm, vm = _empty_memory()
        ctx = _build_memory_context(instant, sm, vm)
        assert "Previous frame:" not in ctx


class TestBuildPrompt:
    """Test prompt assembly with memory context and delta strategies."""

    def test_full_no_context(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        sm, vm = _empty_memory()
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=None,
        )
        assert "## Scene Composition" in prompt

    def test_full_with_video_memory(self) -> None:
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        sm, _ = _empty_memory()
        vm = VideoMemory(text="Topics covered so far")
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=None,
        )
        assert "Video context:" in prompt
        assert "Topics covered so far" in prompt

    def test_explicit_delta_with_prev(self) -> None:
        """Explicit strategy uses delta prompt when prev_result given."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.EXPLICIT
        sm, vm = _empty_memory()
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
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=prev,
        )
        assert "ONLY what changed" in prompt
        assert "Full description here" in prompt

    def test_conditional_with_prev(self) -> None:
        """Conditional strategy includes similarity hint."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.CONDITIONAL
        sm, vm = _empty_memory()
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
        frame = _make_frame(change_class=ChangeClass.LOW, dhash_dist=0.05)
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=prev,
            frame=frame,
        )
        assert "CHANGES ONLY:" in prompt
        assert "Previous content" in prompt
        assert "95%" in prompt
        assert "LOW change" in prompt

    def test_conditional_without_frame_still_works(self) -> None:
        """Conditional prompt works when frame is not provided."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.CONDITIONAL
        sm, vm = _empty_memory()
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
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=prev,
        )
        assert "CHANGES ONLY:" in prompt
        assert "Pixel-level" not in prompt

    def test_none_strategy_ignores_prev(self) -> None:
        """NONE strategy always uses full prompt."""
        analyzer = VisualAnalyzer.__new__(VisualAnalyzer)
        analyzer._delta_strategy = DeltaStrategy.NONE
        sm, vm = _empty_memory()
        prompt = analyzer._build_prompt(
            instant=None,
            scene_memory=sm,
            video_memory=vm,
            prev_result=None,
        )
        assert "## Scene Composition" in prompt


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
