"""Tests for local_scrape_web heavy step."""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import ScrapedContent, ScrapeWebParams
from course_supporter.ingestion.scrape_web import local_scrape_web


def _mock_trafilatura(
    raw_html: str | None = "<html><body>Hello</body></html>",
    extracted: str | None = "Hello",
) -> MagicMock:
    """Create a mock trafilatura module with pre-configured returns."""
    mock_module = MagicMock()
    mock_module.fetch_url.return_value = raw_html
    mock_module.extract.return_value = extracted
    return mock_module


@pytest.fixture(autouse=True)
def _patch_trafilatura() -> Iterator[MagicMock]:
    """Patch trafilatura in sys.modules for all tests by default."""
    mock = _mock_trafilatura()
    with patch.dict("sys.modules", {"trafilatura": mock}):
        yield mock


class TestLocalScrapeWebSuccess:
    async def test_returns_scraped_content(self) -> None:
        """Produces ScrapedContent with text and raw_html."""
        result = await local_scrape_web("https://example.com", ScrapeWebParams())

        assert isinstance(result, ScrapedContent)
        assert result.text == "Hello"
        assert result.raw_html == "<html><body>Hello</body></html>"

    async def test_passes_include_tables_to_extract(self) -> None:
        """include_tables param is forwarded to trafilatura.extract."""
        mock = _mock_trafilatura()
        with patch.dict("sys.modules", {"trafilatura": mock}):
            await local_scrape_web(
                "https://example.com",
                ScrapeWebParams(include_tables=False),
            )

        call_kwargs = mock.extract.call_args
        assert call_kwargs[1]["include_tables"] is False

    async def test_passes_include_comments_to_extract(self) -> None:
        """include_comments param is forwarded to trafilatura.extract."""
        mock = _mock_trafilatura()
        with patch.dict("sys.modules", {"trafilatura": mock}):
            await local_scrape_web(
                "https://example.com",
                ScrapeWebParams(include_comments=True),
            )

        call_kwargs = mock.extract.call_args
        assert call_kwargs[1]["include_comments"] is True

    async def test_passes_url_to_fetch(self) -> None:
        """URL is forwarded to trafilatura.fetch_url."""
        mock = _mock_trafilatura()
        with patch.dict("sys.modules", {"trafilatura": mock}):
            await local_scrape_web("https://example.com/page", ScrapeWebParams())

        mock.fetch_url.assert_called_once_with("https://example.com/page")

    async def test_structlog_events(self) -> None:
        """Logs web_scraping_start and web_scraping_done events."""
        mock = _mock_trafilatura()
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            patch("course_supporter.ingestion.scrape_web.structlog") as mock_log,
        ):
            logger = MagicMock()
            mock_log.get_logger.return_value.bind.return_value = logger

            await local_scrape_web("https://example.com", ScrapeWebParams())

        logger.info.assert_any_call("web_scraping_start")
        logger.info.assert_any_call(
            "web_scraping_done",
            text_length=5,
            has_content=True,
        )


class TestLocalScrapeWebEdgeCases:
    async def test_extract_returns_none_gives_empty_text(self) -> None:
        """When extract returns None, text is empty string."""
        mock = _mock_trafilatura(extracted=None)
        with patch.dict("sys.modules", {"trafilatura": mock}):
            result = await local_scrape_web("https://example.com", ScrapeWebParams())

        assert result.text == ""
        assert result.raw_html == "<html><body>Hello</body></html>"

    async def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped from extracted text."""
        mock = _mock_trafilatura(extracted="  content with spaces  \n")
        with patch.dict("sys.modules", {"trafilatura": mock}):
            result = await local_scrape_web("https://example.com", ScrapeWebParams())

        assert result.text == "content with spaces"

    async def test_empty_extraction_has_content_false(self) -> None:
        """When extract returns empty string, has_content is False in log."""
        mock = _mock_trafilatura(extracted="")
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            patch("course_supporter.ingestion.scrape_web.structlog") as mock_log,
        ):
            logger = MagicMock()
            mock_log.get_logger.return_value.bind.return_value = logger

            await local_scrape_web("https://example.com", ScrapeWebParams())

        logger.info.assert_any_call(
            "web_scraping_done",
            text_length=0,
            has_content=False,
        )


class TestLocalScrapeWebErrors:
    async def test_fetch_returns_none_raises(self) -> None:
        """When fetch_url returns None, raises ProcessingError."""
        mock = _mock_trafilatura(raw_html=None)
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            pytest.raises(ProcessingError, match="no response"),
        ):
            await local_scrape_web("https://bad.example.com", ScrapeWebParams())

    async def test_trafilatura_not_installed(self) -> None:
        """When trafilatura is not installed, raises ProcessingError."""
        with (
            patch.dict("sys.modules", {"trafilatura": None}),
            pytest.raises(ProcessingError, match="trafilatura is not installed"),
        ):
            await local_scrape_web("https://example.com", ScrapeWebParams())

    async def test_fetch_url_raises_exception(self) -> None:
        """When fetch_url raises, wraps in ProcessingError."""
        mock = _mock_trafilatura()
        mock.fetch_url.side_effect = ConnectionError("timeout")
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            pytest.raises(ProcessingError, match="Failed to fetch URL"),
        ):
            await local_scrape_web("https://example.com", ScrapeWebParams())

    async def test_extract_raises_exception(self) -> None:
        """When extract raises, wraps in ProcessingError."""
        mock = _mock_trafilatura()
        mock.extract.side_effect = ValueError("parse error")
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            pytest.raises(ProcessingError, match="Content extraction failed"),
        ):
            await local_scrape_web("https://example.com", ScrapeWebParams())

    async def test_fetch_error_logs_event(self) -> None:
        """Fetch failure logs web_scraping_failed event."""
        mock = _mock_trafilatura()
        mock.fetch_url.side_effect = ConnectionError("refused")
        with (
            patch.dict("sys.modules", {"trafilatura": mock}),
            patch("course_supporter.ingestion.scrape_web.structlog") as mock_log,
        ):
            logger = MagicMock()
            mock_log.get_logger.return_value.bind.return_value = logger

            with pytest.raises(ProcessingError):
                await local_scrape_web("https://example.com", ScrapeWebParams())

        logger.error.assert_called_once_with("web_scraping_failed", error="refused")


class TestScrapeWebParamsValidation:
    def test_default_values(self) -> None:
        """Default params include tables but not comments."""
        params = ScrapeWebParams()
        assert params.include_tables is True
        assert params.include_comments is False

    def test_custom_values(self) -> None:
        """Custom params override defaults."""
        params = ScrapeWebParams(include_tables=False, include_comments=True)
        assert params.include_tables is False
        assert params.include_comments is True
