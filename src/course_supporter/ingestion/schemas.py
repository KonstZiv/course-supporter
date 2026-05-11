"""Draft Pydantic models for the ingestion pipeline (KD-2.1-M).

In-flight data carriers between processor stages and repository
``.create()`` calls. Distinct from ORM models in
:mod:`course_supporter.storage.orm`: drafts are short-lived
Pydantic validation containers; ORM models are session-bound
persistence entities with content_hash, lifecycle status, and
soft-delete state.

Phase 2.1 commit 4.5 introduces these drafts as the typed return
shape for :meth:`MaterialProcessor.process_macro` (Pass 2a) and
:meth:`MaterialProcessor.process_detail` (Pass 2b). Concrete
processor overrides (TextProcessor / WebProcessor) arrive in
commits 5 and 7; ORM materialisation through
``DocumentSummaryRepository.create`` arrives in commit 5.

All non-list fields are required (no Optional). LLM callers must
emit complete payloads; partial output fails fast through Pydantic
validation rather than silently propagating None into ORM rows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentSegmentDraft(BaseModel):
    """In-flight segment draft (Pass 2b output, pre-ORM)."""

    order: int = Field(description="0-indexed position within parent summary.")
    start_pos: int = Field(
        description="Absolute start char offset in the source unit.",
    )
    end_pos: int = Field(
        description="Absolute end char offset (must be > start_pos).",
    )
    content: str = Field(description="Cleaned or sliced segment content.")
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in this segment (KD-gamma).",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught in this segment.",
    )


class DocumentSummaryDraft(BaseModel):
    """In-flight summary draft (Pass 2a output, pre-ORM)."""

    title: str = Field(description="Self-contained summary title (<=128 chars).")
    description: str = Field(description="Brief description (<=512 chars).")
    main_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings taught in depth across the document (KD-gamma).",
    )
    secondary_concepts: list[str] = Field(
        default_factory=list,
        description="Concept strings mentioned but not taught in depth.",
    )
    content_char_count: int = Field(
        description=(
            "Total char count over child DocumentSegment.content "
            "(size-metric for weighting per vision section 2.2)."
        ),
    )
    segments: list[DocumentSegmentDraft] = Field(
        default_factory=list,
        description=(
            "Child segment drafts. Pass 2a may emit empty list; "
            "Pass 2b appends drafts before ORM materialisation."
        ),
    )
