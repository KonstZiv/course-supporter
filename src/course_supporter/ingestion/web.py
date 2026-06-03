"""Web processor — thin orchestrator over ScrapeWebFunc."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from pydantic import ValidationError

from course_supporter.ingestion.base import (
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.heavy_steps import (
    ScrapeWebFunc,
    ScrapeWebParams,
)
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.language import display_name
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument

logger = structlog.get_logger()


class WebProcessor(MaterialProcessor):
    """Process web pages by delegating to an injected scraping function.

    Uses ``ScrapeWebFunc`` for content extraction (default: ``local_scrape_web``).
    Raw HTML is saved as content_snapshot for re-processing.
    """

    def __init__(
        self,
        *,
        scrape_func: ScrapeWebFunc | None = None,
    ) -> None:
        self._scrape_func = scrape_func or self._default_scrape_func()

    @staticmethod
    def _default_scrape_func() -> ScrapeWebFunc:
        """Lazy-import local_scrape_web as the default implementation."""
        from course_supporter.ingestion.scrape_web import local_scrape_web

        return local_scrape_web

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        if source.source_type != SourceType.WEB:
            raise UnsupportedFormatError(
                f"WebProcessor expects 'web', got '{source.source_type}'"
            )

        url = source.source_url
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        logger.info("web_processing_start", url=url, domain=domain)

        # 1. Delegate to heavy step
        scraped = await self._scrape_func(url, ScrapeWebParams())

        # 2. Split into chunks
        chunks = self._text_to_chunks(scraped.text) if scraped.text else []

        fetched_at = datetime.now(UTC).isoformat()

        logger.info(
            "web_processing_done",
            url=url,
            chunk_count=len(chunks),
        )

        return SourceDocument(
            source_type=SourceType.WEB,
            source_url=url,
            title=source.filename or domain,
            chunks=chunks,
            metadata={
                "domain": domain,
                "fetched_at": fetched_at,
                "content_snapshot": scraped.raw_html,
            },
        )

    @staticmethod
    def _text_to_chunks(text: str) -> list[ContentChunk]:
        """Split extracted text into content chunks.

        Splits on double newlines to create paragraph-like chunks.
        """
        chunks: list[ContentChunk] = []
        paragraphs = text.strip().split("\n\n")

        for idx, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.WEB_CONTENT,
                    text=para,
                    index=idx,
                )
            )

        return chunks

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Pass 2a -- premium LLM extracts concept structure (KD-2.1-A).

        Concatenates chunk text into a single body and routes the
        resulting document through the ``pass_2a_mapping`` stage.
        Returns a :class:`DocumentSummaryDraft`; segment drafts are
        retained on the result for Phase 2.1 C7 (Pass 2b) consumption
        and are not materialised here.

        Document-level ``main_concepts`` / ``secondary_concepts`` are
        derived algorithmically as ``union + dedup`` over the per-segment
        concepts (vision.md §2.2, KD-2.1-O). The LLM is asked to emit
        concepts only at segment level — this method assembles the
        document-level view by sorted set-union and applies the
        conflict rule: any concept that appears as ``main`` in at
        least one segment stays in ``main_concepts`` and is removed
        from ``secondary_concepts``.
        """
        text = doc.assemble_text()
        if not text.strip():
            msg = "Cannot run Pass 2a on empty document (no content chunks)"
            raise ProcessingError(msg)
        reference_text_length = len(text)
        parsed: dict[str, DocumentSummaryDraft] = {}

        def _coverage_validator(content: str) -> None:
            """StageRouter response_validator hook (fixup 2.1.7.2).

            Mirrors :meth:`TextProcessor.process_macro` -- translates
            a Pydantic ``ValidationError`` into
            :class:`StructuralRetryError` so the router's existing
            instructor-style retry path fires.
            """
            try:
                draft_local = DocumentSummaryDraft.model_validate_json(
                    content,
                    context={"reference_text_length": reference_text_length},
                )
            except ValidationError as exc:
                error_types = sorted({e.get("type", "unknown") for e in exc.errors()})
                first = exc.errors()[0]
                first_loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "pass2a.validation.failed",
                    source_type=doc.source_type.value,
                    validation_error_types=error_types,
                    first_error_msg=first.get("msg", ""),
                    first_error_loc=first_loc,
                    reference_text_length=reference_text_length,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {first_loc or '<root>'}). "
                    "Regenerate the response with valid output."
                )
                raise StructuralRetryError(feedback) from exc
            parsed["draft"] = draft_local

        result = await router.execute_for_stage(
            "pass_2a_mapping",
            response_validator=_coverage_validator,
            expects_json=True,
            text=text,
            language=display_name(doc.language) if doc.language else None,
        )
        draft = parsed["draft"]
        logger.debug(
            "pass2a.validation.ok",
            source_type=doc.source_type.value,
            provider=result.provider_used,
            model=result.model_used,
            attempt_count=result.attempt_count,
            reference_text_length=reference_text_length,
        )

        # Algorithmic aggregation of document-level concepts from
        # per-segment concepts (vision.md §2.2, KD-2.1-O). LLM emits
        # concepts only at segment level; ``DocumentSummary.main_concepts``
        # is the sorted set-union over all segments. Conflict rule:
        # any concept that appears as ``main`` in at least one segment
        # stays in ``main_concepts`` and is removed from
        # ``secondary_concepts``.
        all_main: set[str] = set()
        all_secondary: set[str] = set()
        for seg in draft.segments:
            all_main.update(seg.main_concepts)
            all_secondary.update(seg.secondary_concepts)
        all_secondary -= all_main
        draft.main_concepts = sorted(all_main)
        draft.secondary_concepts = sorted(all_secondary)

        return draft

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
    ) -> list[DocumentSegmentDraft]:
        """Pass 2b -- algorithmic slice over Pass 2a offsets (KD-2.1-O).

        Zero LLM calls. Mirrors ``TextProcessor.process_detail``: slices
        ``doc.assemble_text()`` per draft offset pair. Reference text is
        the same string the mapping LLM saw in Pass 2a. Non-None
        ``draft.content`` is passed through verbatim (defensive).
        """
        reference_text = doc.assemble_text()
        filled: list[DocumentSegmentDraft] = []
        for draft in summary_draft.segments:
            if draft.content is not None:
                filled.append(draft)
                continue
            sliced = reference_text[draft.start_pos : draft.end_pos]
            filled.append(draft.model_copy(update={"content": sliced}))
        return filled
