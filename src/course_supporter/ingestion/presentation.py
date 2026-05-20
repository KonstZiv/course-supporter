"""PresentationProcessor — three-stage presentation ingestion (KD-2.3-F/I/K/O/Q).

Phase 2.3 sub-area #4. Refactors the legacy single-stage processor into
the three-stage pipeline shared with ``AudioProcessor`` / text / web:

* :meth:`process_raw` — normalize PPTX → PDF (LibreOffice) when needed,
  extract per-slide text + render @150dpi PNG (PyMuPDF), emit one
  ``SLIDE_TEXT`` chunk per slide WHERE ``raw_text`` is non-empty
  (KD-2.3-O / v0.3 N3 empty-slide filter; symmetric with
  ``audio.py:280``), then assert the critical invariant
  (KD-2.3-F + KD-2.2-E precedent). The full per-slide sequence
  (including image-only slides) is stashed on ``self._slide_raw`` for
  ``process_macro`` — an instance-state hand-off (KD-2.3-O "internal
  state") that works because the ARQ orchestrator calls all three
  stages on the same processor instance within one
  ``arq_ingest_material`` invocation.
* :meth:`process_macro` — Pass 1 per-slide vision description via
  ``asyncio.gather`` over ALL slides (bounded by a concurrency
  semaphore), then Pass 2a slide-grouping. Pass 1 partial failure is
  fail-fast (KD-2.3-F): any per-slide exception raises a
  ``ProcessingError`` with the first failing slide + total count.
  Pass 2a output is JSON validated against ``PresentationPass2aResult``
  through the StageRouter ``response_validator`` (markdown-fence strip
  per KD-2.3-T Path 3 carry-forward; Mistral may wrap JSON in fences).
* :meth:`process_detail` — Pass 2b algorithmic slice via the
  ``chars_per_slide_cumsum`` bridge (KD-2.3-Q). Pass 2c is SKIPPED per
  KD2d (per-slide VD output is clean by construction).

Single-dependency design (Design 2, ratified): no constructor args.
The ``StageRouter`` arrives via the ``process_macro`` method argument
(``TextProcessor`` / ``WebProcessor`` codebase precedent), not via
``__init__``. ``process_raw`` needs no LLM — only LibreOffice +
PyMuPDF.

LibreOffice 26.2.x is a worker-image system dependency (``soffice`` on
PATH) required only for the PPTX/PPT normalize path; the PDF path uses
PyMuPDF alone. The Dockerfile install lands in sub-area #7.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import fitz
import structlog
from pydantic import ValidationError

from course_supporter.ingestion.base import (
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
    PresentationPass2aResult,
)
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

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".pptx", ".ppt"})
_PPTX_EXTENSIONS = frozenset({".pptx", ".ppt"})

# Worker-side slide-count cap (CA-3). Enforced post-LibreOffice convert in
# _extract_pdf_pages -- the authoritative gate for ``.ppt`` (which the
# HTTP-side check in api/routes/documents.py cannot inspect, as python-pptx
# reads only OOXML). The HTTP gate keeps its own independent copy of this
# limit (documents.py:_MAX_PRESENTATION_SLIDES) -- two independent
# enforcement layers, deliberately not consolidated.
_MAX_PRESENTATION_SLIDES = 100

_PASS_1_STAGE_NAME = "presentation_pass_1_vision"  # noqa: S105
_PASS_2A_STAGE_NAME = "presentation_pass_2a_mapping"  # noqa: S105

# Pass 1 dispatches one vision call per slide; the semaphore bounds
# concurrent in-flight LLM calls to the spike v2 calibration value
# (CONCURRENCY=6) so a 30-slide deck does not fan out 30 simultaneous
# provider calls and trip rate limits. Not configurable in Phase 2.3.
_PASS_1_CONCURRENCY = 6

# LibreOffice headless conversion timeout; a single slide deck should
# normalize well under this even on a cold-start soffice.
_NORMALIZE_TIMEOUT_SEC = 60

# Render resolution for per-slide PNGs handed to the Pass 1 vision
# model (spike v2 used 150dpi; tokens stayed at 1.1-2.5k/slide).
_RENDER_DPI = 150

# Markdown-fence strip for Pass 2a JSON (KD-2.3-T Path 3 carry-forward).
# Mistral may wrap its JSON in ```json fences despite the prompt
# directive; this mirrors ``DashScopeProvider._strip_markdown_json``.
# Revisit = DD-2.3-AG (response_format Path 1 if fence emission proves costly).
# Kept local — sharing a one-line regex across providers/processors
# would be premature coupling.
_MD_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences from a JSON response if present."""
    match = _MD_JSON_RE.search(text)
    return match.group(1).strip() if match else text.strip()


@dataclass(frozen=True, slots=True)
class SlideRaw:
    """Per-slide extraction carrier (internal, process_raw → process_macro).

    ``slide_number`` is 1-indexed. ``raw_text`` is the PyMuPDF
    text-layer extraction (may be empty for image-only slides).
    ``image_bytes`` is the rendered @150dpi PNG handed to the Pass 1
    vision model for every slide regardless of ``raw_text`` emptiness.
    """

    slide_number: int
    raw_text: str
    image_bytes: bytes


def chars_per_slide_cumsum(chunks: list[ContentChunk]) -> list[int]:
    """Slide-chunk → char-offset bridge for presentation Pass 2b (KD-2.3-Q).

    Works over the EMITTED chunks (post v0.3 N3 empty-text filter). The
    presentation ``assemble_text`` separator is ``"\\n\\n"`` (2 chars),
    owned by each non-last chunk. Invariant:
    ``cumsum[len(chunks)] == len(assemble_text(doc))``.

    Empty ``chunks`` returns ``[0]`` — the single-element baseline.

    Examples:
        >>> chars_per_slide_cumsum([])
        [0]
    """
    cumsum = [0]
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        separator_len = 0 if i == last_idx else 2
        cumsum.append(cumsum[-1] + len(chunk.text) + separator_len)
    return cumsum


class PresentationProcessor(MaterialProcessor):
    """Presentation source-type processor — three-stage ingestion.

    See module docstring for architectural context (single-dependency
    Design 2, instance-state slide_raw hand-off, LibreOffice worker
    dependency, fail-fast Pass 1, Pass 2c skip).
    """

    def __init__(self) -> None:
        super().__init__()
        # Set by ``process_raw``, read by ``process_macro``. The
        # orchestrator runs both on the same instance within one ARQ
        # task; a ``None`` value at ``process_macro`` time signals a
        # call-ordering violation (cache-miss guard, analog to
        # ``audio.py`` word-cache miss).
        # DD-2.3-AJ: explicit-state-passing refactor (Phase 2.5, all processors).
        self._slide_raw: list[SlideRaw] | None = None

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Stage 1 — extract per-slide text + render images → SourceDocument.

        The ``router`` parameter is accepted for ABC symmetry but not
        used (presentation extraction is LibreOffice + PyMuPDF, no LLM).

        Flow:
        1. Validate ``source_type`` + extension.
        2. PPTX/PPT → normalize to PDF bytes via LibreOffice; PDF →
           read bytes directly.
        3. Extract per-slide ``SlideRaw`` (text + @150dpi PNG).
        4. Stash full sequence on ``self._slide_raw`` for Pass 1.
        5. Emit one ``SLIDE_TEXT`` chunk per slide WHERE ``raw_text``
           is non-empty (v0.3 N3).
        6. Assert the critical invariant (KD-2.3-F).
        """
        if source.source_type != SourceType.PRESENTATION:
            raise UnsupportedFormatError(
                f"PresentationProcessor expects 'presentation', "
                f"got '{source.source_type}'"
            )

        path = Path(source.source_url)
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported presentation format: {ext}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        logger.info("presentation_processing_start", source_url=source.source_url)

        if ext in _PPTX_EXTENSIONS:
            pdf_bytes = await self._normalize_pptx_to_pdf(str(path))
        else:
            pdf_bytes = await asyncio.to_thread(path.read_bytes)

        slide_raw = await asyncio.to_thread(self._extract_pdf_pages, pdf_bytes)
        self._slide_raw = slide_raw

        chunks = [
            ContentChunk(
                chunk_type=ChunkType.SLIDE_TEXT,
                text=sr.raw_text,
                index=sr.slide_number,
                metadata={"slide_number": sr.slide_number},
            )
            for sr in slide_raw
            if sr.raw_text  # v0.3 N3 — image-only slides emit no chunk
        ]

        doc = SourceDocument(
            source_type=source.source_type,
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
            metadata={"slide_count": len(slide_raw)},
        )

        expected = "\n\n".join(chunk.text for chunk in doc.chunks if chunk.text)
        actual = doc.assemble_text()
        assert actual == expected, (
            f"PresentationProcessor invariant violated: "
            f"assemble_text() (len={len(actual)}) != "
            f"'\\n\\n'.join(non-empty chunks) (len={len(expected)}). "
            f"Check the v0.3 N3 empty-slide filter and the "
            f"models/source.py presentation separator branch."
        )

        logger.info(
            "presentation_processing_done",
            source_url=source.source_url,
            slide_count=len(slide_raw),
            emitted_chunk_count=len(chunks),
        )
        return doc

    async def _normalize_pptx_to_pdf(self, input_path: str) -> bytes:
        """Convert a PPTX/PPT to PDF bytes via headless LibreOffice (KD-2.3-I).

        The conversion runs inside a ``tempfile.TemporaryDirectory``
        context (v0.3 N4): the output PDF and the per-invocation
        LibreOffice user profile are removed when the method returns
        OR raises — so a downstream ``_extract_pdf_pages`` failure
        never leaks the temp directory. A dedicated ``-env:
        UserInstallation`` per invocation isolates concurrent
        conversions.
        """
        with tempfile.TemporaryDirectory(prefix="pptx_normalize_") as tmpdir:
            profile_uri = (Path(tmpdir) / "lo_profile").as_uri()
            # DD-2.3-AK: no CPU/memory rlimit on soffice (Phase 2.5 ops-hardening).
            proc = await asyncio.create_subprocess_exec(
                "soffice",
                "--headless",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "pdf",
                "--outdir",
                tmpdir,
                input_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_NORMALIZE_TIMEOUT_SEC
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ProcessingError(
                    f"PPTX_NORMALIZE_TIMEOUT: soffice exceeded "
                    f"{_NORMALIZE_TIMEOUT_SEC}s converting {input_path}"
                ) from exc

            if proc.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[:500]
                raise ProcessingError(
                    f"PPTX_NORMALIZE_FAILED: soffice exited "
                    f"{proc.returncode} for {input_path}: {detail}"
                )

            pdf_path = Path(tmpdir) / (Path(input_path).stem + ".pdf")
            if not pdf_path.is_file():
                raise ProcessingError(
                    f"PPTX_NORMALIZE_FAILED: expected output "
                    f"{pdf_path.name} not produced by soffice for {input_path}"
                )
            return pdf_path.read_bytes()

    def _extract_pdf_pages(self, pdf_bytes: bytes) -> list[SlideRaw]:
        """Extract per-slide text + render @150dpi PNG via PyMuPDF (KD-2.3-K).

        Synchronous (PyMuPDF is blocking); called via
        ``asyncio.to_thread`` from ``process_raw``. Canonical worker
        text-extraction surface — python-pptx is NOT used here.
        """
        slides: list[SlideRaw] = []
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if doc.page_count > _MAX_PRESENTATION_SLIDES:
                # CA-3 fail-fast (KD-2.3-F): reject before the text+render
                # loop so an over-limit deck wastes no PyMuPDF rendering.
                raise ProcessingError(
                    f"PRESENTATION_SLIDE_LIMIT: {doc.page_count} slides "
                    f"exceeds limit {_MAX_PRESENTATION_SLIDES}"
                )
            for i in range(doc.page_count):
                page = doc.load_page(i)
                raw_text = page.get_text("text").strip()
                pixmap = page.get_pixmap(dpi=_RENDER_DPI)
                image_bytes = pixmap.tobytes("png")
                slides.append(
                    SlideRaw(
                        slide_number=i + 1,
                        raw_text=raw_text,
                        image_bytes=image_bytes,
                    )
                )
        finally:
            doc.close()
        return slides

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Pass 1 (per-slide VD) + Pass 2a (slide grouping) → summary draft.

        Reads the full per-slide sequence from ``self._slide_raw``
        (populated by ``process_raw``). Pass 1 runs for ALL slides,
        including image-only ones (their VD descriptions inform Pass 2a
        grouping even though they emit no chunk). Pass 2a output is
        validated and mapped onto ``DocumentSegmentDraft`` offsets via
        the ``chars_per_slide_cumsum`` bridge.

        Presentation Pass 2a carries no concepts (KD2d); the resulting
        ``DocumentSummaryDraft`` has empty ``main_concepts`` /
        ``secondary_concepts``.
        """
        slide_raw = self._slide_raw
        if slide_raw is None:
            raise ProcessingError(
                "PresentationProcessor.process_macro requires "
                "process_raw to populate slide_raw first; the "
                "orchestrator must call process_raw before "
                "process_macro on the same processor instance."
            )

        descriptions = await self._run_pass_1(slide_raw, router)
        pass2a = await self._run_pass_2a(doc, slide_raw, descriptions, router)

        segment_drafts = self._build_segment_drafts(doc, pass2a)

        return DocumentSummaryDraft(
            title=pass2a.title or "",
            description=pass2a.description,
            main_concepts=[],
            secondary_concepts=[],
            segments=segment_drafts,
        )

    async def _run_pass_1(
        self,
        slide_raw: list[SlideRaw],
        router: StageRouter,
    ) -> list[str]:
        """Pass 1 — per-slide vision description, bounded-concurrency gather.

        Fail-fast (KD-2.3-F): any per-slide exception raises a
        ``ProcessingError`` naming the first failing slide and the
        total failure count. Plain-text output per slide (no JSON, no
        response_validator) — symmetric with the audio Pass 2c shape.
        """
        sem = asyncio.Semaphore(_PASS_1_CONCURRENCY)

        async def _describe(sr: SlideRaw) -> str:
            async with sem:
                result = await router.execute_for_stage(
                    _PASS_1_STAGE_NAME,
                    contents=[sr.image_bytes],
                    slide_number=sr.slide_number,
                    raw_text=sr.raw_text,
                )
                return result.content

        results = await asyncio.gather(
            *(_describe(sr) for sr in slide_raw),
            return_exceptions=True,
        )

        failures = [
            (sr.slide_number, r)
            for sr, r in zip(slide_raw, results, strict=True)
            if isinstance(r, BaseException)
        ]
        if failures:
            first_slide, first_exc = failures[0]
            raise ProcessingError(
                f"Pass 1 failed on slide {first_slide}: {first_exc}; "
                f"{len(failures)}/{len(slide_raw)} slides total failed"
            )

        return [r for r in results if not isinstance(r, BaseException)]

    async def _run_pass_2a(
        self,
        doc: SourceDocument,
        slide_raw: list[SlideRaw],
        descriptions: list[str],
        router: StageRouter,
    ) -> PresentationPass2aResult:
        """Pass 2a — slide grouping with JSON validation + fence strip.

        Builds the numbered per-slide content block from the Pass 1
        descriptions, dispatches the mapping stage, and validates the
        response against ``PresentationPass2aResult`` (markdown fences
        stripped first per KD-2.3-T Path 3). A ``ValidationError`` is
        translated to ``StructuralRetryError`` so the ladder retry +
        fallback chain handles structural failures.
        """
        n_slides = len(slide_raw)
        slides_json = "\n".join(
            f"[Slide {sr.slide_number}] {desc}"
            for sr, desc in zip(slide_raw, descriptions, strict=True)
        )

        parsed: dict[str, PresentationPass2aResult] = {}

        def _validator(content: str) -> None:
            stripped = _strip_json_fences(content)
            try:
                local = PresentationPass2aResult.model_validate_json(
                    stripped,
                    context={"n_slides": n_slides},
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                first_loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "presentation_pass2a.validation.failed",
                    n_slides=n_slides,
                    first_error_msg=first.get("msg", ""),
                    first_error_loc=first_loc,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {first_loc or '<root>'}). "
                    "Regenerate the response as valid JSON matching the schema."
                )
                raise StructuralRetryError(feedback) from exc
            parsed["result"] = local

        router_result = await router.execute_for_stage(
            _PASS_2A_STAGE_NAME,
            response_validator=_validator,
            file_title=doc.title,
            n_slides=n_slides,
            slides_json=slides_json,
        )
        result = parsed["result"]
        logger.debug(
            "presentation_pass2a.validation.ok",
            provider=router_result.provider_used,
            model=router_result.model_used,
            attempt_count=router_result.attempt_count,
            n_slides=n_slides,
            segment_count=len(result.segments),
        )
        return result

    @staticmethod
    def _build_segment_drafts(
        doc: SourceDocument,
        pass2a: PresentationPass2aResult,
    ) -> list[DocumentSegmentDraft]:
        """Map Pass 2a slide ranges onto char-offset segment drafts (KD-2.3-Q).

        Each segment's ``[start_slide, end_slide]`` range is resolved to
        the emitted chunks whose ``slide_number`` falls inside it; the
        char offsets come from the ``chars_per_slide_cumsum`` bridge.
        Slides are emitted in order, so a contiguous slide range maps to
        a contiguous chunk-index block, preserving the
        ``DocumentSummaryDraft`` contiguity invariant.

        Degenerate guard: a segment whose slide range contains no
        emitted chunk (all slides image-only) cannot be sliced into a
        non-empty char range and fails the ``DocumentSegmentDraft``
        ``end_pos > start_pos`` invariant. Such a segment raises a
        ``ProcessingError`` (fail-fast) rather than producing an invalid
        draft. Not exercised by the ratified test set; surfaced for
        vision-side handling decision.
        """
        cumsum = chars_per_slide_cumsum(doc.chunks)
        slide_to_idx = {
            int(chunk.metadata["slide_number"]): i for i, chunk in enumerate(doc.chunks)
        }
        emitted_slides = sorted(slide_to_idx)

        drafts: list[DocumentSegmentDraft] = []
        for order, seg in enumerate(pass2a.segments):
            idxs = [
                slide_to_idx[s]
                for s in emitted_slides
                if seg.start_slide <= s <= seg.end_slide
            ]
            if not idxs:
                raise ProcessingError(
                    f"PRESENTATION_EMPTY_SEGMENT: Pass 2a segment slides "
                    f"[{seg.start_slide}-{seg.end_slide}] contains no "
                    f"extractable text (all slides image-only); cannot map "
                    f"to char offsets for Pass 2b slicing."
                )
            drafts.append(
                DocumentSegmentDraft(
                    order=order,
                    start_pos=cumsum[min(idxs)],
                    end_pos=cumsum[max(idxs) + 1],
                    title=seg.title,
                    description=seg.description,
                    start_slide=seg.start_slide,
                    end_slide=seg.end_slide,
                )
            )
        return drafts

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
    ) -> list[DocumentSegmentDraft]:
        """Pass 2b — algorithmic slice; Pass 2c SKIPPED (KD2d).

        For each segment draft carrying ``content=None``, slice the
        content from ``doc.assemble_text()[start_pos:end_pos]`` using
        the offsets populated in ``process_macro`` via the cumsum
        bridge. Pass 2c is skipped — per-slide VD output is clean by
        construction, so there is no denoise step.
        """
        reference_text = doc.assemble_text()
        sliced: list[DocumentSegmentDraft] = []
        for draft in summary_draft.segments:
            if draft.content is not None:
                sliced.append(draft)
                continue
            content = reference_text[draft.start_pos : draft.end_pos]
            sliced.append(draft.model_copy(update={"content": content}))
        return sliced
