"""Tests for VisualAnalyzer (Eyes step) — no real API calls."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.vd.schemas import (
    ChangeClass,
    DeltaStrategy,
    EyesResult,
    FrameSource,
    InstantMemory,
    SampledFrame,
    Scene,
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
    frame_id: str = "frame_001_10s",
    ts: float = 10.0,
    change_class: ChangeClass = ChangeClass.LOW,
    dhash_dist: float = 0.07,
) -> SampledFrame:
    """Helper to create a SampledFrame for tests."""
    return SampledFrame(
        frame_id=frame_id,
        filename=f"{frame_id}.jpg",
        timestamp_sec=ts,
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

    def test_none_never_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.NONE)
        assert not a._should_use_delta(
            _make_frame(change_class=ChangeClass.LOW),
            self._make_prev(),
        )

    def test_no_prev_never_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            _make_frame(change_class=ChangeClass.LOW),
            None,
        )

    def test_explicit_low_is_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert a._should_use_delta(
            _make_frame(change_class=ChangeClass.LOW),
            self._make_prev(),
        )

    def test_explicit_medium_is_delta(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert a._should_use_delta(
            _make_frame(change_class=ChangeClass.MEDIUM),
            self._make_prev(),
        )

    def test_explicit_boundary_is_full(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            _make_frame(change_class=ChangeClass.BOUNDARY),
            self._make_prev(),
        )

    def test_explicit_first_is_full(self) -> None:
        a = self._make_analyzer(DeltaStrategy.EXPLICIT)
        assert not a._should_use_delta(
            _make_frame(change_class=ChangeClass.FIRST),
            self._make_prev(),
        )

    def test_conditional_always_with_prev(self) -> None:
        a = self._make_analyzer(DeltaStrategy.CONDITIONAL)
        assert a._should_use_delta(
            _make_frame(change_class=ChangeClass.BOUNDARY),
            self._make_prev(),
        )


# -- Helper utilities for LLM-mocked tests --------------------------------


@pytest.fixture()
def mock_router() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_rate_limiter() -> AsyncMock:
    limiter = AsyncMock()
    limiter.acquire = AsyncMock()
    return limiter


@pytest.fixture()
def analyzer(mock_router: AsyncMock, mock_rate_limiter: AsyncMock) -> VisualAnalyzer:
    return VisualAnalyzer(mock_router, mock_rate_limiter)


# -- VisualAnalyzer.__init__ / model property ------------------------------


class TestAnalyzerInit:
    def test_model_property(self, analyzer: VisualAnalyzer) -> None:
        assert analyzer.model == "gemini-2.5-flash"

    def test_custom_model(
        self, mock_router: AsyncMock, mock_rate_limiter: AsyncMock
    ) -> None:
        a = VisualAnalyzer(mock_router, mock_rate_limiter, model="custom-model")
        assert a.model == "custom-model"


# -- _find_frame -----------------------------------------------------------


class TestFindFrame:
    def test_direct_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "frame.jpg"
            p.write_bytes(b"img")
            assert VisualAnalyzer._find_frame(Path(d), "frame.jpg") == p

    def test_subdir_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            p = sub / "frame.jpg"
            p.write_bytes(b"img")
            assert VisualAnalyzer._find_frame(Path(d), "frame.jpg") == p

    def test_not_found_raises(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            pytest.raises(FileNotFoundError),
        ):
            VisualAnalyzer._find_frame(Path(d), "missing.jpg")


# -- _call_llm (mocked Gemini) --------------------------------------------


class TestCallLlm:
    async def test_success(self, analyzer: VisualAnalyzer) -> None:
        mock_resp = MagicMock(
            content="LLM response",
            model_id="gemini-2.5-flash",
            latency_ms=100,
            tokens_in=100,
            tokens_out=50,
        )
        analyzer._router.complete = AsyncMock(return_value=mock_resp)

        result = await analyzer._call_llm([b"fake-image"], prompt="describe")

        assert result["text"] == "LLM response"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    async def test_rate_limit_retries(self, analyzer: VisualAnalyzer) -> None:
        rate_err = Exception("429 Resource exhausted")
        mock_resp = MagicMock(
            content="OK",
            model_id="gemini-2.5-flash",
            latency_ms=100,
            tokens_in=10,
            tokens_out=5,
        )
        analyzer._router.complete = AsyncMock(side_effect=[rate_err, mock_resp])

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await analyzer._call_llm([b"fake-image"], prompt="describe")

        assert result["text"] == "OK"
        assert analyzer._router.complete.await_count == 2


# -- analyze_scene (fully mocked) -----------------------------------------


def _mock_eyes_result(frame_id: str, ts: float = 0.0) -> EyesResult:
    return EyesResult(
        frame_id=frame_id,
        timestamp_sec=ts,
        scene_id=0,
        response="## Scene Composition\n**Setting:** screen_recording",
        n_images=1,
        latency_sec=1.0,
        input_tokens=100,
        output_tokens=50,
        description="Code editor",
    )


class TestAnalyzeScene:
    async def test_single_frame_scene(self, analyzer: VisualAnalyzer) -> None:
        scene = Scene(scene_id=0, frame_ids=["f0"], start_sec=0.0, end_sec=5.0)
        frames = [_make_frame(frame_id="f0", ts=0.0)]

        with patch.object(
            analyzer,
            "_analyze_frame",
            new=AsyncMock(return_value=_mock_eyes_result("f0")),
        ):
            results, sm = await analyzer.analyze_scene(scene, frames, Path("/tmp"))

        assert len(results) == 1
        assert results[0].frame_id == "f0"
        assert sm is None  # no memory pipeline

    async def test_multi_frame_with_memory(
        self, mock_router: AsyncMock, mock_rate_limiter: AsyncMock
    ) -> None:
        mock_memory = AsyncMock()
        mock_memory.update_scene_memory = AsyncMock(
            return_value=SceneMemory(scene_id=0, summary="Updated", frames_seen=1)
        )
        a = VisualAnalyzer(mock_router, mock_rate_limiter, memory=mock_memory)
        scene = Scene(
            scene_id=0,
            frame_ids=["f0", "f1"],
            start_sec=0.0,
            end_sec=10.0,
        )
        frames = [
            _make_frame(frame_id="f0", ts=0.0),
            _make_frame(frame_id="f1", ts=5.0),
        ]

        call_count = 0

        async def mock_analyze(*_a: object, **_k: object) -> EyesResult:
            nonlocal call_count
            fid = frames[call_count].frame_id
            call_count += 1
            return _mock_eyes_result(fid)

        with patch.object(a, "_analyze_frame", new=mock_analyze):
            results, sm = await a.analyze_scene(scene, frames, Path("/tmp"))

        assert len(results) == 2
        assert sm is not None
        assert mock_memory.update_scene_memory.await_count >= 1

    async def test_returns_none_scene_memory_without_pipeline(
        self, analyzer: VisualAnalyzer
    ) -> None:
        """Without MemoryPipeline, scene_memory is None."""
        scene = Scene(scene_id=0, frame_ids=["f0"], start_sec=0.0, end_sec=5.0)
        frames = [_make_frame(frame_id="f0", ts=0.0)]

        with patch.object(
            analyzer,
            "_analyze_frame",
            new=AsyncMock(return_value=_mock_eyes_result("f0")),
        ):
            _, sm = await analyzer.analyze_scene(scene, frames, Path("/tmp"))

        assert sm is None
