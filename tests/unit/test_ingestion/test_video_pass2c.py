"""Unit tests for video Pass 2c selective denoise (task 2.4.7, step_7).

The cheap text ladder is mocked (no network); the mock StageRouter drives
the ``response_validator`` closure with a cleaned string, mirroring the audio
Pass 2c test pattern. Tests cover the mechanism the task ratified: selective
routing on ``noisy``, the ``content``-only contract (``visual_content`` clean
by construction, KD2d), the empty-noisy short circuit, the ``content`` guard,
ladder-exhaust propagation (R1), and the ValidationError → StructuralRetryError
translation.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.schemas import DocumentSegmentDraft, VisualSceneRef
from course_supporter.ingestion.video_pipeline import steps
from course_supporter.llm.error_categories import StructuralRetryError


def _mock_router(cleaned: str) -> AsyncMock:
    """StageRouter mock that drives the Pass 2c validator with cleaned text."""
    router = AsyncMock()

    async def _execute(
        stage: str,
        *,
        response_validator: Callable[[str], None],
        **template_kwargs: object,
    ) -> MagicMock:
        response_validator(cleaned)
        out = MagicMock()
        out.provider_used = "mock-llm"
        out.model_used = "mock-model"
        out.attempt_count = 1
        return out

    router.execute_for_stage.side_effect = _execute
    return router


def _draft(
    order: int,
    *,
    content: str | None,
    noisy: bool,
    visual: list[VisualSceneRef] | None = None,
) -> DocumentSegmentDraft:
    return DocumentSegmentDraft(
        order=order,
        start_pos=order * 10,
        end_pos=order * 10 + 5,
        description="d",
        content=content,
        noisy=noisy,
        visual_content=visual,
    )


class TestStep7SelectiveDenoise:
    """step_7 routes only noisy segments through the cheap text ladder."""

    async def test_noisy_segment_is_denoised(self) -> None:
        router = _mock_router("cleaned text")
        draft = _draft(0, content="raw noisy text", noisy=True)

        result = await steps.step_7_pass2c_cleanup([draft], stage_router=router)

        assert result[0].content == "cleaned text"
        router.execute_for_stage.assert_awaited_once()
        # The stage id and the prompt-kwargs the denoise call carries.
        kwargs = router.execute_for_stage.await_args.kwargs
        assert router.execute_for_stage.await_args.args[0] == "video_pass_2c_denoise"
        assert kwargs["content"] == "raw noisy text"
        assert "main_concepts_json" in kwargs

    async def test_non_noisy_segment_keeps_raw_slice(self) -> None:
        router = _mock_router("WILL_NOT_BE_USED")
        draft = _draft(0, content="raw clean text", noisy=False)

        result = await steps.step_7_pass2c_cleanup([draft], stage_router=router)

        assert result[0].content == "raw clean text"
        router.execute_for_stage.assert_not_called()

    async def test_routes_only_the_noisy_subset(self) -> None:
        router = _mock_router("cleaned")
        segments = [
            _draft(0, content="part1", noisy=False),
            _draft(1, content="part2", noisy=True),
            _draft(2, content="part3", noisy=False),
            _draft(3, content="part4", noisy=True),
        ]

        result = await steps.step_7_pass2c_cleanup(segments, stage_router=router)

        assert [s.content for s in result] == ["part1", "cleaned", "part3", "cleaned"]
        assert router.execute_for_stage.await_count == 2

    async def test_empty_noisy_set_returns_as_is_zero_calls(self) -> None:
        router = _mock_router("unused")
        segments = [
            _draft(0, content="a", noisy=False),
            _draft(1, content="b", noisy=False),
        ]

        result = await steps.step_7_pass2c_cleanup(segments, stage_router=router)

        # Same objects returned (no model_copy), zero LLM calls.
        assert result is segments
        router.execute_for_stage.assert_not_called()

    async def test_noisy_but_empty_content_is_guarded(self) -> None:
        """A noisy segment whose content is falsy is skipped (cannot occur
        after Pass 2b, but the ``d.noisy and d.content`` guard mirrors audio).
        """
        router = _mock_router("unused")
        draft = _draft(0, content=None, noisy=True)

        result = await steps.step_7_pass2c_cleanup([draft], stage_router=router)

        assert result[0].content is None
        router.execute_for_stage.assert_not_called()


class TestStep7VisualUntouched:
    """KD2d — Pass 2c cleans ONLY ``content``; ``visual_content`` passes through."""

    async def test_visual_content_untouched_on_denoised_segment(self) -> None:
        router = _mock_router("cleaned narration")
        visual = [
            VisualSceneRef(position_ms=0, description="slide 1", kind="anchor"),
            VisualSceneRef(position_ms=4000, description="slide 2", kind="diff"),
        ]
        draft = _draft(0, content="raw noisy narration", noisy=True, visual=visual)

        result = await steps.step_7_pass2c_cleanup([draft], stage_router=router)

        assert result[0].content == "cleaned narration"
        # The visual stream is byte-identical — denoise structurally cannot
        # touch it (model_copy updates only ``content``).
        assert result[0].visual_content == visual
        # The denoise prompt-kwargs never carry the visual descriptions.
        kwargs = router.execute_for_stage.await_args.kwargs
        assert "slide 1" not in str(kwargs)


class TestStep7FailurePropagation:
    """Ladder exhaustion + validator translation (R1 taxonomy)."""

    async def test_ladder_exhaust_propagates_processing_error(self) -> None:
        router = AsyncMock()
        router.execute_for_stage.side_effect = ProcessingError("ladder exhausted")
        draft = _draft(0, content="raw noisy", noisy=True)

        with pytest.raises(ProcessingError, match="ladder exhausted"):
            await steps.step_7_pass2c_cleanup([draft], stage_router=router)

    async def test_validation_error_becomes_structural_retry(self) -> None:
        """A non-string LLM payload fails ``AudioPass2cResult`` validation and
        is translated to ``StructuralRetryError`` (so the real ladder retries).
        """
        router = AsyncMock()

        async def _execute(
            stage: str,
            *,
            response_validator: Callable[[str], None],
            **template_kwargs: object,
        ) -> MagicMock:
            response_validator(None)  # type: ignore[arg-type]
            return MagicMock()

        router.execute_for_stage.side_effect = _execute
        draft = _draft(0, content="raw noisy", noisy=True)

        with pytest.raises(StructuralRetryError):
            await steps.step_7_pass2c_cleanup([draft], stage_router=router)
