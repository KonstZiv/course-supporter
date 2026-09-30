"""Pass 2a mapping for text and web documents (shared by both processors).

The model reads ``doc.assemble_text()`` as numbered lines and returns each
segment as an inclusive line range (:class:`TextMappingResponse`). The
server checks the line cover, converts the ranges into char offsets over
that same string and builds the :class:`DocumentSummaryDraft` that Pass 2b,
the paragraph anchors and the repositories consume unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from course_supporter.concept_dedup import dedupe_concepts, subtract_by_key
from course_supporter.ingestion.base import CategorisedProcessingError
from course_supporter.ingestion.line_ranges import (
    NumberedLine,
    line_range_to_span,
    number_lines,
    render_numbered,
)
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
    TextMappingResponse,
)
from course_supporter.language import display_name
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.security.exceptions import ErrorCategory

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.models.source import SourceDocument

logger = structlog.get_logger()

_STAGE_NAME = "pass_2a_mapping"


async def map_text_document(
    doc: SourceDocument,
    router: StageRouter,
) -> DocumentSummaryDraft:
    """Run Pass 2a for a text/web document (KD-2.1-A, KD-2.1-O).

    Returns a :class:`DocumentSummaryDraft` whose segments carry char
    offsets into ``doc.assemble_text()`` and no ``content`` (Pass 2b fills
    it). Document-level ``main_concepts`` / ``secondary_concepts`` are the
    union + dedup over the per-segment concepts; a concept that is main in
    at least one segment is dropped from the secondary list.

    Raises:
        CategorisedProcessingError: The document has no text.
    """
    text = doc.assemble_text()
    if not text.strip():
        msg = "Cannot run Pass 2a on empty document (no content chunks)"
        raise CategorisedProcessingError(ErrorCategory.EMPTY_DOCUMENT, msg)
    lines = number_lines(text)
    line_count = len(lines)
    parsed: dict[str, TextMappingResponse] = {}

    def _coverage_validator(content: str) -> None:
        """StageRouter ``response_validator`` hook.

        Translates a Pydantic ``ValidationError`` into
        :class:`StructuralRetryError` so the router's instructor-style
        retry fires (one retry on the same model with the feedback
        appended; ladder fallback on the second failure). The feedback
        carries every error, not only the first.
        """
        try:
            response = TextMappingResponse.model_validate_json(
                content, context={"line_count": line_count}
            )
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0]
            error_types = sorted({e.get("type", "unknown") for e in errors})
            logger.warning(
                "pass2a.validation.failed",
                source_type=doc.source_type.value,
                validation_error_types=error_types,
                first_error_msg=first.get("msg", ""),
                first_error_loc=".".join(str(x) for x in first.get("loc", [])),
                line_count=line_count,
                reference_text_length=len(text),
            )
            feedback = "; ".join(_describe_error(e) for e in errors)
            raise StructuralRetryError(
                f"{feedback}. Regenerate the whole response with valid output"
            ) from exc
        parsed["response"] = response

    result = await router.execute_for_stage(
        _STAGE_NAME,
        response_validator=_coverage_validator,
        expects_json=True,
        text=render_numbered(text, lines),
        line_count=line_count,
        language=display_name(doc.language) if doc.language else None,
    )
    # The validator stored the parsed response on the winning attempt; if
    # execute_for_stage returned, it succeeded at least once.
    response = parsed["response"]
    logger.debug(
        "pass2a.validation.ok",
        source_type=doc.source_type.value,
        provider=result.provider_used,
        model=result.model_used,
        attempt_count=result.attempt_count,
        line_count=line_count,
        reference_text_length=len(text),
    )

    draft = to_summary_draft(response, lines, reference_text_length=len(text))

    # Algorithmic aggregation of document-level concepts (vision.md §2.2,
    # KD-2.1-O). Concepts accumulate WITH repeats so dedupe can count
    # occurrences; main and secondary are consolidated separately.
    all_main: list[str] = []
    all_secondary: list[str] = []
    for seg in draft.segments:
        all_main.extend(seg.main_concepts)
        all_secondary.extend(seg.secondary_concepts)
    main_concepts = dedupe_concepts(all_main)
    secondary_concepts = subtract_by_key(dedupe_concepts(all_secondary), main_concepts)
    draft.main_concepts = sorted(main_concepts)
    draft.secondary_concepts = sorted(secondary_concepts)
    return draft


def to_summary_draft(
    response: TextMappingResponse,
    lines: list[NumberedLine],
    *,
    reference_text_length: int,
) -> DocumentSummaryDraft:
    """Convert a line-cover-valid response into the char-offset draft.

    The draft is validated with the ``reference_text_length`` context, so
    the unchanged :class:`DocumentSummaryDraft` invariants (contiguous,
    starts at 0, ends at the text length) hold by construction and are
    still checked.
    """
    segments = []
    for order, seg in enumerate(response.segments):
        start_pos, end_pos = line_range_to_span(lines, seg.start_line, seg.end_line)
        segments.append(
            DocumentSegmentDraft(
                order=order,
                start_pos=start_pos,
                end_pos=end_pos,
                title=seg.title,
                description=seg.description,
                main_concepts=seg.main_concepts,
                secondary_concepts=seg.secondary_concepts,
            )
        )
    return DocumentSummaryDraft.model_validate(
        {
            "title": response.title,
            "description": response.description,
            "segments": segments,
        },
        context={"reference_text_length": reference_text_length},
    )


def _describe_error(error: ErrorDetails) -> str:
    """One Pydantic error as retry feedback: message plus field path.

    The model-level cover check has no field path and already names the
    segments, so it goes out as its bare message.
    """
    msg = error.get("msg", "validation error").removeprefix("Value error, ")
    loc = ".".join(str(x) for x in error.get("loc", []))
    return f"{msg} (field: {loc})" if loc else msg
