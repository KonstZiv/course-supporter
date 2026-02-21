"""Local web scraping via trafilatura — heavy step implementation.

Standalone async function that fetches a URL and extracts main content
using trafilatura. Conforms to ``ScrapeWebFunc`` protocol from
:mod:`course_supporter.ingestion.heavy_steps`.

No DB, S3, or ORM dependencies — pure url-in, content-out.
Can be swapped for a Lambda-based or headless-browser implementation later.
"""

from __future__ import annotations

import asyncio

import structlog

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.heavy_steps import ScrapedContent, ScrapeWebParams


async def local_scrape_web(
    url: str,
    params: ScrapeWebParams,
) -> ScrapedContent:
    """Fetch and extract main content from a web page.

    Runs trafilatura's fetch and extract in a thread pool to avoid
    blocking the event loop (both are sync blocking I/O / CPU-bound).

    Args:
        url: URL to fetch and extract content from.
        params: Scraping parameters (include_tables, include_comments).

    Returns:
        Scraped content with extracted text and raw HTML.

    Raises:
        ProcessingError: If trafilatura is not installed, fetch fails,
            or an unexpected error occurs.
    """
    logger = structlog.get_logger().bind(url=url)
    logger.info("web_scraping_start")

    try:
        import trafilatura
    except ImportError:
        raise ProcessingError(
            "trafilatura is not installed. Install with: uv add trafilatura"
        ) from None

    loop = asyncio.get_running_loop()

    try:
        raw_html: str | None = await loop.run_in_executor(
            None, trafilatura.fetch_url, url
        )
    except Exception as exc:
        logger.error("web_scraping_failed", error=str(exc))
        raise ProcessingError(f"Failed to fetch URL: {exc}") from exc

    if raw_html is None:
        logger.error("web_scraping_failed", error="fetch returned None")
        raise ProcessingError(f"Failed to fetch URL (no response): {url}")

    try:
        extracted: str | None = await loop.run_in_executor(
            None,
            lambda: trafilatura.extract(
                raw_html,
                include_tables=params.include_tables,
                include_comments=params.include_comments,
            ),
        )
    except Exception as exc:
        logger.error("web_scraping_failed", error=str(exc))
        raise ProcessingError(f"Content extraction failed: {exc}") from exc

    text = (extracted or "").strip()

    logger.info(
        "web_scraping_done",
        text_length=len(text),
        has_content=bool(text),
    )

    return ScrapedContent(text=text, raw_html=raw_html)
