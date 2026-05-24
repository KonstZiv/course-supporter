"""Heavy step protocols and param/result models.

Defines typed contracts for all heavy (serverless-ready) operations:
- Web scraping (trafilatura)

Each heavy step is a plain async callable with a clean contract:
structured params in → structured result out. No DB, no S3, no ORM.
Processors become orchestrators that call these functions via DI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Web scraping (trafilatura / future headless browser)
# ---------------------------------------------------------------------------


class ScrapeWebParams(BaseModel):
    """Parameters for web content extraction."""

    include_tables: bool = Field(
        default=True,
        description="Whether to include table content in extraction.",
    )
    include_comments: bool = Field(
        default=False,
        description="Whether to include user comments.",
    )


class ScrapedContent(BaseModel):
    """Result of web page scraping."""

    text: str = Field(description="Extracted main content as plain text.")
    raw_html: str = Field(description="Raw HTML for snapshot / re-processing.")


ScrapeWebFunc = Callable[
    [str, ScrapeWebParams],
    Awaitable[ScrapedContent],
]
"""Async callable: (url, params) → ScrapedContent.

First argument is the URL to fetch and extract content from.
"""
