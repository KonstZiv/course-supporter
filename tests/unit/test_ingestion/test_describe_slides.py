"""Tests for local_describe_slides heavy step."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.describe_slides import local_describe_slides
from course_supporter.ingestion.heavy_steps import (
    DEFAULT_SLIDE_DESCRIPTION_PROMPT,
    DescribeSlidesParams,
    SlideDescription,
)


@pytest.fixture(autouse=True)
def _fake_pdf_exists() -> Iterator[None]:
    """Patch Path.exists to return True for fake PDF paths in tests."""
    with patch("course_supporter.ingestion.describe_slides.Path") as mock_path:
        mock_path.return_value.exists.return_value = True
        yield


def _mock_fitz_doc(page_count: int = 3) -> MagicMock:
    """Create a mock fitz document with pages that render to PNG."""
    doc = MagicMock()
    doc.__len__ = lambda self: page_count

    pages: list[MagicMock] = []
    for _ in range(page_count):
        page = MagicMock()
        pixmap = MagicMock()
        pixmap.tobytes.return_value = b"fake-png-bytes"
        page.get_pixmap.return_value = pixmap
        pages.append(page)

    doc.__getitem__ = lambda self, idx: pages[idx]
    return doc


def _mock_fitz_module(doc: MagicMock) -> MagicMock:
    """Create a mock fitz module that returns the given doc on open."""
    mock_module = MagicMock()
    mock_module.open.return_value = doc
    mock_module.Page = MagicMock  # for type annotation
    return mock_module


def _mock_router(
    responses: list[str] | None = None,
    *,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Create a mock ModelRouter.

    Args:
        responses: List of response content strings (one per slide).
        side_effect: Exception to raise on every call.
    """
    router = AsyncMock()
    if side_effect is not None:
        router.complete.side_effect = side_effect
    elif responses is not None:
        results = []
        for text in responses:
            resp = MagicMock()
            resp.content = text
            results.append(resp)
        router.complete.side_effect = results
    else:
        resp = MagicMock()
        resp.content = "Slide description"
        router.complete.return_value = resp
    return router


class TestLocalDescribeSlidesSuccess:
    async def test_returns_descriptions_for_all_pages(self) -> None:
        """Produces SlideDescription for each page."""
        doc = _mock_fitz_doc(page_count=2)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["First slide", "Second slide"])

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        assert len(result) == 2
        assert all(isinstance(d, SlideDescription) for d in result)
        assert result[0].slide_number == 1
        assert result[0].description == "First slide"
        assert result[1].slide_number == 2
        assert result[1].description == "Second slide"

    async def test_uses_configured_dpi(self) -> None:
        """Renders pages at the configured DPI."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Description"])
        params = DescribeSlidesParams(dpi=300)

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            await local_describe_slides("/tmp/slides.pdf", params, router=router)

        page = doc[0]
        page.get_pixmap.assert_called_once_with(dpi=300)

    async def test_uses_configured_prompt(self) -> None:
        """Passes custom prompt to router.complete."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Description"])
        custom_prompt = "Describe the code on this slide."
        params = DescribeSlidesParams(prompt=custom_prompt)

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            await local_describe_slides("/tmp/slides.pdf", params, router=router)

        call_kwargs = router.complete.call_args
        assert call_kwargs.kwargs["prompt"] == custom_prompt

    async def test_default_prompt_used(self) -> None:
        """Default prompt matches DEFAULT_SLIDE_DESCRIPTION_PROMPT."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Description"])

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        call_kwargs = router.complete.call_args
        assert call_kwargs.kwargs["prompt"] == DEFAULT_SLIDE_DESCRIPTION_PROMPT

    async def test_sends_image_bytes_as_contents(self) -> None:
        """PNG bytes are sent via contents parameter."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Description"])

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        call_kwargs = router.complete.call_args
        assert call_kwargs.kwargs["contents"] == [b"fake-png-bytes"]

    async def test_action_is_presentation_analysis(self) -> None:
        """Router is called with action='presentation_analysis'."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Description"])

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        call_kwargs = router.complete.call_args
        assert call_kwargs.kwargs["action"] == "presentation_analysis"


class TestLocalDescribeSlidesEdgeCases:
    async def test_empty_pdf(self) -> None:
        """PDF with 0 pages returns empty list."""
        doc = _mock_fitz_doc(page_count=0)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router()

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/empty.pdf", DescribeSlidesParams(), router=router
            )

        assert result == []
        router.complete.assert_not_called()

    async def test_empty_response_skipped(self) -> None:
        """Slides with empty LLM response are skipped."""
        doc = _mock_fitz_doc(page_count=2)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(["Valid description", "   "])

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        assert len(result) == 1
        assert result[0].slide_number == 1

    async def test_none_response_content_skipped(self) -> None:
        """Slides with None content are skipped."""
        doc = _mock_fitz_doc(page_count=1)
        fitz_mod = _mock_fitz_module(doc)
        resp = MagicMock()
        resp.content = None
        router = AsyncMock()
        router.complete.return_value = resp

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        assert result == []

    async def test_single_slide_failure_does_not_crash_batch(self) -> None:
        """If one slide fails, others are still processed."""
        doc = _mock_fitz_doc(page_count=3)
        fitz_mod = _mock_fitz_module(doc)

        resp1 = MagicMock()
        resp1.content = "Slide 1 ok"
        resp3 = MagicMock()
        resp3.content = "Slide 3 ok"

        router = AsyncMock()
        router.complete.side_effect = [
            resp1,
            RuntimeError("Vision API timeout"),
            resp3,
        ]

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        assert len(result) == 2
        assert result[0].slide_number == 1
        assert result[1].slide_number == 3

    async def test_all_slides_fail_returns_empty(self) -> None:
        """If all slides fail, returns empty list (no crash)."""
        doc = _mock_fitz_doc(page_count=2)
        fitz_mod = _mock_fitz_module(doc)
        router = _mock_router(side_effect=RuntimeError("API down"))

        with patch.dict("sys.modules", {"fitz": fitz_mod}):
            result = await local_describe_slides(
                "/tmp/slides.pdf", DescribeSlidesParams(), router=router
            )

        assert result == []


class TestLocalDescribeSlidesErrors:
    async def test_file_not_found(self) -> None:
        """Non-existent PDF raises ProcessingError."""
        with (
            patch("course_supporter.ingestion.describe_slides.Path") as mock_path,
            pytest.raises(ProcessingError, match="PDF file not found"),
        ):
            mock_path.return_value.exists.return_value = False
            await local_describe_slides(
                "/nonexistent/slides.pdf",
                DescribeSlidesParams(),
                router=AsyncMock(),
            )

    async def test_fitz_not_installed(self) -> None:
        """When fitz is not installed, raises ProcessingError."""
        with (
            patch.dict("sys.modules", {"fitz": None}),
            pytest.raises(ProcessingError, match=r"PyMuPDF.*not installed"),
        ):
            await local_describe_slides(
                "/tmp/slides.pdf",
                DescribeSlidesParams(),
                router=AsyncMock(),
            )

    async def test_pdf_open_failure(self) -> None:
        """Corrupted PDF raises ProcessingError."""
        fitz_mod = MagicMock()
        fitz_mod.open.side_effect = RuntimeError("corrupted file")

        with (
            patch.dict("sys.modules", {"fitz": fitz_mod}),
            pytest.raises(ProcessingError, match="Failed to open PDF"),
        ):
            await local_describe_slides(
                "/tmp/bad.pdf",
                DescribeSlidesParams(),
                router=AsyncMock(),
            )


class TestDescribeSlidesParamsValidation:
    def test_default_params(self) -> None:
        """Default params use DPI=150 and standard prompt."""
        params = DescribeSlidesParams()
        assert params.dpi == 150
        assert params.prompt == DEFAULT_SLIDE_DESCRIPTION_PROMPT

    def test_custom_dpi(self) -> None:
        """Custom DPI is accepted."""
        params = DescribeSlidesParams(dpi=300)
        assert params.dpi == 300

    def test_zero_dpi_rejected(self) -> None:
        """DPI must be > 0."""
        with pytest.raises(ValueError):
            DescribeSlidesParams(dpi=0)

    def test_negative_dpi_rejected(self) -> None:
        """Negative DPI is rejected."""
        with pytest.raises(ValueError):
            DescribeSlidesParams(dpi=-1)
